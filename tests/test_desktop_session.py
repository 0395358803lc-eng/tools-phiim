import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from flow_story_studio.desktop import DesktopSession, _workspace_override


def test_workspace_override_is_explicit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FLOW_STUDIO_DATA_DIR", raising=False)
    monkeypatch.setenv("FLOW_STUDIO_WORKSPACE_DIR", str(tmp_path))

    assert _workspace_override() == tmp_path.resolve()


def test_folder_dialog_cancel_and_selection(monkeypatch, tmp_path: Path) -> None:
    session = DesktopSession(
        SimpleNamespace(FOLDER_DIALOG=20), credential_root=tmp_path / "credentials"
    )
    session._window = SimpleNamespace(create_file_dialog=lambda _: None)
    assert session.choose_workspace() == {"ok": False}

    session._window = SimpleNamespace(create_file_dialog=lambda _: [str(tmp_path)])
    monkeypatch.setattr(session, "_start_backend", lambda path: "http://127.0.0.1:12345")
    selected = session.choose_workspace()

    assert selected == {
        "ok": True,
        "url": "http://127.0.0.1:12345",
        "workspace": str(tmp_path.resolve()),
    }


def test_javascript_bridge_exposes_only_folder_chooser(tmp_path: Path) -> None:
    session = DesktopSession(
        SimpleNamespace(FOLDER_DIALOG=20), credential_root=tmp_path / "credentials"
    )
    public_methods = {
        name
        for name in dir(session)
        if not name.startswith("_") and callable(getattr(session, name))
    }

    assert public_methods == {"choose_workspace"}


def test_selected_workspace_backs_the_desktop_api(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FLOW_STUDIO_SESSION_ID", raising=False)
    monkeypatch.setenv("FLOW_STUDIO_DATA_DIR", str(tmp_path))
    session = DesktopSession(
        SimpleNamespace(FOLDER_DIALOG=20), credential_root=tmp_path / "credentials"
    )
    try:
        url = session._start_backend(tmp_path)
        with urllib.request.urlopen(f"{url}/api/session", timeout=5) as response:
            payload = json.load(response)
        assert payload["fresh_start"] is True
        assert Path(payload["workspace"]) == tmp_path.resolve()
        assert (tmp_path / "projects").is_dir()
    finally:
        session._shutdown()
