from fastapi.testclient import TestClient

from flow_story_studio.main import create_app
from flow_story_studio.storage import ProjectStorage


def test_app_runs_and_render_is_blocked_without_backend(tmp_path) -> None:
    app = create_app(ProjectStorage(tmp_path / "projects"))
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.json()["render"]["configured"] is False

        created = client.post(
            "/api/projects/analyze",
            json={
                "name": "No renderer",
                "original_text": (
                    "Một người bước vào phòng. Anh nhìn quanh rồi ngồi xuống ghế."
                ),
            },
        )
        assert created.status_code == 201
        project = created.json()
        assert project["settings"]["provider"] == "unconfigured"

        blocked = client.post(
            f"/api/projects/{project['id']}/generate",
            json={"scene_ids": []},
        )
        assert blocked.status_code == 409
        assert "render provider" in blocked.json()["detail"]
