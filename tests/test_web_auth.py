from fastapi.testclient import TestClient

from flow_story_studio.main import create_app
from flow_story_studio.storage import ProjectStorage


def test_web_auth_blocks_api_until_login(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TH_MEDIA_WEB_AUTH", "1")
    monkeypatch.setenv("TH_MEDIA_WEB_USERNAME", "admin")
    monkeypatch.setenv("TH_MEDIA_WEB_PASSWORD", "test-web-password")
    monkeypatch.setenv("TH_MEDIA_COOKIE_SECURE", "0")

    app = create_app(ProjectStorage(tmp_path / "projects"))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/projects").status_code == 401
        assert client.get("/").status_code == 200
        assert "/login" in str(client.get("/", follow_redirects=False).headers.get("location", ""))

        bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert bad.status_code == 401

        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test-web-password"},
        )
        assert login.status_code == 200
        assert client.get("/api/projects").status_code == 200

        logout = client.delete("/api/auth/session")
        assert logout.status_code == 200
        assert client.get("/api/projects").status_code == 401
