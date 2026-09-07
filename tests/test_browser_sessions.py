from pathlib import Path

from fastapi.testclient import TestClient

from flow_story_studio.browser_sessions import GoogleFlowSessionManager
from flow_story_studio.main import create_app
from flow_story_studio.storage import ProjectStorage


def _manager(tmp_path: Path):
    chrome = tmp_path / "chrome.exe"
    chrome.write_bytes(b"fake")
    launches: list[tuple[Path, Path]] = []

    def launch(chrome_path: Path, profile_dir: Path) -> None:
        launches.append((chrome_path, profile_dir))

    return (
        GoogleFlowSessionManager(
            tmp_path / "sessions",
            chrome_path=chrome,
            launcher=launch,
        ),
        launches,
    )


def test_google_flow_session_replacement_is_transactional(tmp_path: Path) -> None:
    manager, launches = _manager(tmp_path)

    initial = manager.status()
    assert initial["configured"] is False
    assert initial["pending"] is None

    pending_one = manager.create_new()
    first_id = pending_one["pending"]["id"]
    assert pending_one["configured"] is False
    assert len(launches) == 1

    active_one = manager.activate_pending()
    assert active_one["configured"] is True
    assert active_one["active"]["id"] == first_id
    first_profile = manager.active_profile_dir()
    assert first_profile.is_dir()

    pending_two = manager.create_new()
    second_id = pending_two["pending"]["id"]
    assert second_id != first_id
    assert pending_two["active"]["id"] == first_id
    assert pending_two["pending"]["id"] == second_id
    assert len(launches) == 2

    active_two = manager.activate_pending()
    assert active_two["active"]["id"] == second_id
    assert active_two["pending"] is None
    assert manager.active_profile_dir() != first_profile


def test_cancel_pending_keeps_current_google_flow_session(tmp_path: Path) -> None:
    manager, _launches = _manager(tmp_path)
    first = manager.create_new()["pending"]["id"]
    manager.activate_pending()
    second = manager.create_new()["pending"]["id"]

    status = manager.cancel_pending()

    assert status["active"]["id"] == first
    assert status["pending"] is None
    assert second != first


def test_google_flow_session_api_uses_isolated_browser_profiles(tmp_path: Path) -> None:
    manager, launches = _manager(tmp_path)
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage, browser_session_manager=manager)

    with TestClient(app) as client:
        status = client.get("/api/google-flow/session")
        assert status.status_code == 200
        assert status.json()["storage_mode"] == "isolated_chrome_profile"

        created = client.post("/api/google-flow/session/new")
        assert created.status_code == 200
        pending_id = created.json()["pending"]["id"]
        assert launches

        reopened = client.post("/api/google-flow/session/pending/open")
        assert reopened.status_code == 200
        assert reopened.json()["pending"]["id"] == pending_id
        assert len(launches) == 2

        activated = client.post("/api/google-flow/session/pending/activate")
        assert activated.status_code == 200
        assert activated.json()["active"]["id"] == pending_id

        opened = client.post("/api/google-flow/session/open")
        assert opened.status_code == 200
        assert opened.json()["configured"] is True
        assert len(launches) == 3


def test_new_google_flow_session_requires_pending_resolution(tmp_path: Path) -> None:
    manager, _launches = _manager(tmp_path)
    manager.create_new()
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage, browser_session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/google-flow/session/new")
        assert response.status_code == 409
        assert "đang chờ xác nhận" in response.json()["detail"]
