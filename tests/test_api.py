from pathlib import Path

import pytest
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

        exported = client.get(f"/api/projects/{project_id}/render-prompts.zip")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"


def test_mock_render_queue(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects/analyze",
            json={"name": "Render Demo", "original_text": TEXT, "settings": {"provider": "mock"}},
        ).json()
        scene_id = project["scenes"][0]["id"]

        blocked = client.post(
            f"/api/projects/{project['id']}/generate",
            json={"scene_ids": [scene_id]},
        )
        assert blocked.status_code == 409
        assert "Image Plan" in blocked.json()["detail"]

        stored = storage.get(project["id"])
        assert stored is not None
        stored.scenes[0].image_plan.status = "Ready"
        storage.save(stored)

        queued = client.post(
            f"/api/projects/{project['id']}/generate",
            json={"scene_ids": [scene_id]},
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
                "provider": "future-renderer",
                "video_model": "future-model",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["settings"]["provider"] == "future-renderer"


def test_video_settings_and_continuity_are_locked_during_render(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage)
    with TestClient(app) as client:
        project_data = client.post(
            "/api/projects/analyze",
            json={"name": "Render mutation lock", "original_text": TEXT, "settings": {}},
        ).json()
        project = storage.get(project_data["id"])
        assert project is not None
        project.scenes[0].status = "Generating"
        storage.save(project)

        settings = client.patch(
            f"/api/projects/{project.id}/video-settings",
            json={
                "provider": "future-renderer",
                "video_model": "future-model",
            },
        )
        assert settings.status_code == 423
        assert "render đang chạy" in settings.json()["detail"]

        continuity = client.post(f"/api/projects/{project.id}/continuity")
        assert continuity.status_code == 423
        assert "render đang chạy" in continuity.json()["detail"]


def test_changing_video_settings_invalidates_old_render_evidence(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage)
    with TestClient(app) as client:
        project_data = client.post(
            "/api/projects/analyze",
            json={"name": "Settings evidence", "original_text": TEXT, "settings": {}},
        ).json()
        project = storage.get(project_data["id"])
        assert project is not None
        scene = project.scenes[0]
        scene.status = "Accepted"
        scene.progress = 100
        scene.result_url = "/old-result"
        scene.result_file = "renders/old.mp4"
        scene.last_frame_file = "references/old.jpg"
        scene.render_provider = project.settings.provider
        scene.render_model = project.settings.video_model
        scene.provider_job_id = "old-job"
        scene.acceptance.status = "Accepted"
        scene.acceptance.score = 100
        scene.visual_qc.status = "Passed"
        project.final_video.status = "Ready"
        storage.save(project)

        updated = client.patch(
            f"/api/projects/{project.id}/video-settings",
            json={
                "provider": "future-renderer",
                "video_model": "future-model",
            },
        )
        assert updated.status_code == 200
        payload = updated.json()
        changed = payload["scenes"][0]
        assert changed["status"] == "Waiting"
        assert changed["result_url"] == ""
        assert changed["result_file"] == ""
        assert changed["last_frame_file"] == ""
        assert changed["render_provider"] == ""
        assert changed["render_model"] == ""
        assert changed["provider_job_id"] == ""
        assert changed["acceptance"]["status"] == "Pending"
        assert changed["visual_qc"]["status"] == "Pending"
        assert payload["final_video"]["status"] == "NotReady"


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


def _make_byte_range_scene(tmp_path: Path, client: TestClient) -> dict:
    project_data = client.post(
        "/api/projects/analyze",
        json={"name": "Video Range Edge", "original_text": TEXT, "settings": {}},
    ).json()
    storage = ProjectStorage(tmp_path / "projects")
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
    return {"url": scene.result_url, "path": target}


@pytest.mark.parametrize(
    "range_header,expected",
    [
        (None, 200),
        ("bytes=0-99", 206),
        ("bytes=100-", 206),
        ("bytes=-500", 206),
        ("bytes=100-199", 206),
        ("bytes=0-999999999", 206),
        ("bytes=1500-3000", 206),
        ("bytes=3000-4000", 416),
        ("bytes=-0", 416),
        ("bytes=-", 416),
        ("bytes=", 416),
        ("bytes=abc-def", 416),
        ("bytes=100-abc", 416),
        ("bytes=abc-100", 416),
        ("bytes=500-100", 416),
        ("bytes=0-99,200-299", 416),
        ("bytes=,", 416),
        ("bytes=-1-", 416),
        ("bytes=1-2-3", 416),
        ("bytes= 100 - 199 ", 416),
        ("items=0-99", 416),
        ("bytes=256-256", 206),
    ],
)
def test_video_range_edges(tmp_path: Path, range_header: str | None, expected: int) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage)
    with TestClient(app) as client:
        scene = _make_byte_range_scene(tmp_path, client)
        headers = {"Range": range_header} if range_header is not None else {}
        response = client.get(scene["url"], headers=headers)
    assert response.status_code == expected
    if expected == 206 and range_header not in (None, "bytes=256-256"):
        assert response.headers["content-range"].startswith("bytes ")
        assert int(response.headers["content-length"]) <= 2048
    if expected == 416 and range_header in ("bytes=3000-4000",):
        assert response.headers.get("content-range") == "bytes */2048"


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


def test_master_reference_upload_is_project_level_vision_approved_and_reused(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeXKiro:
        configured = True

        def set_checkpoint_root(self, _root) -> None:
            return None

    async def approve_master(self, reference, relative_path: str, *, model_id: str = ""):
        assert reference.id.startswith("VIS-")
        assert "/masters/" in relative_path
        assert model_id == "vision-selected"
        return 99, []

    monkeypatch.setattr(
        "flow_story_studio.project_routes.VisualQCAnalyzer.inspect_reference",
        approve_master,
    )

    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage, xkiro_client=FakeXKiro())  # type: ignore[arg-type]
    with TestClient(app) as client:
        created = client.post(
            "/api/projects/analyze",
            json={
                "name": "Master reuse",
                "original_text": TEXT,
                "settings": {"vision_model": "vision-selected"},
            },
        )
        assert created.status_code == 201
        project = created.json()
        reference = project["visual_bible"]["references"][0]
        reference_id = reference["id"]

        stored = storage.get(project["id"])
        assert stored is not None
        dependent = next(
            scene
            for scene in stored.scenes
            if reference_id
            in {
                *scene.visual_plan.character_reference_ids,
                *scene.visual_plan.prop_reference_ids,
                scene.visual_plan.location_reference_id,
            }
        )
        dependent.status = "Accepted"
        dependent.result_url = "/old-result"
        dependent.result_file = "renders/old.mp4"
        dependent.acceptance.status = "Accepted"
        storage.save(stored)

        response = client.post(
            f"/api/projects/{project['id']}/visual-references/{reference_id}/image",
            content=b"\x89PNG\r\n\x1a\nmaster-reference",
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        payload = response.json()
        approved = next(
            item
            for item in payload["visual_bible"]["references"]
            if item["id"] == reference_id
        )
        assert approved["status"] == "approved"
        assert approved["approved_reference"].startswith(
            f"references/{project['id']}/masters/{reference_id}/"
        )
        assert approved["source_scene_id"] == ""

        users = []
        for scene in payload["scenes"]:
            ids = {
                *scene["visual_plan"]["character_reference_ids"],
                *scene["visual_plan"]["prop_reference_ids"],
                scene["visual_plan"]["location_reference_id"],
            }
            if reference_id in ids:
                users.append(scene)
                assert scene["image_plan"]["reference_status"][reference_id] == "approved"
        assert users
        changed = next(scene for scene in users if scene["id"] == dependent.id)
        assert changed["status"] == "Waiting"
        assert changed["result_url"] == ""
        assert changed["result_file"] == ""
        assert changed["acceptance"]["status"] == "Pending"


def test_master_reference_upload_stays_candidate_without_vision(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage)
    with TestClient(app) as client:
        project = client.post(
            "/api/projects/analyze",
            json={"name": "Master candidate", "original_text": TEXT, "settings": {}},
        ).json()
        reference_id = project["visual_bible"]["references"][0]["id"]

        response = client.post(
            f"/api/projects/{project['id']}/visual-references/{reference_id}/image",
            content=b"\x89PNG\r\n\x1a\ncandidate-reference",
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 200
        payload = response.json()
        reference = next(
            item
            for item in payload["visual_bible"]["references"]
            if item["id"] == reference_id
        )
        assert reference["status"] == "candidate"
        assert reference["approved_reference"] == ""
        assert any(
            scene["image_plan"]["status"] == "Blocked"
            for scene in payload["scenes"]
            if reference_id
            in {
                *scene["visual_plan"]["character_reference_ids"],
                *scene["visual_plan"]["prop_reference_ids"],
                scene["visual_plan"]["location_reference_id"],
            }
        )
