import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from flow_story_studio.desktop import (
    DesktopSession,
    _migrate_legacy_credentials,
    _workspace_override,
)


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
        base_url = url.split("/#", 1)[0]
        with urllib.request.urlopen(f"{base_url}/api/session", timeout=5) as response:
            payload = json.load(response)
        assert payload["fresh_start"] is True
        assert Path(payload["workspace"]) == tmp_path.resolve()
        assert (tmp_path / "projects").is_dir()
    finally:
        session._shutdown()


def test_legacy_credentials_migrate_per_file_without_overwrite(tmp_path: Path) -> None:
    new_root = tmp_path / "new"
    legacy_root = tmp_path / "legacy"
    new_root.mkdir()
    legacy_root.mkdir()

    (new_root / "xkiro-api-key.bin").write_bytes(b"new-xkiro")
    (legacy_root / "xkiro-api-key.bin").write_bytes(b"old-xkiro")
    (legacy_root / "google-flow.cookies.bin").write_bytes(b"legacy-flow")

    _migrate_legacy_credentials(new_root, legacy_root)

    assert (new_root / "xkiro-api-key.bin").read_bytes() == b"new-xkiro"
    assert (new_root / "google-flow.cookies.bin").read_bytes() == b"legacy-flow"


def test_legacy_credentials_migrate_per_file(monkeypatch, tmp_path: Path) -> None:
    from flow_story_studio import desktop

    app_root = tmp_path / "TH Media"
    legacy_root = tmp_path / "Flow Story Studio"
    new_secrets = app_root / "secrets"
    legacy_secrets = legacy_root / "secrets"
    new_secrets.mkdir(parents=True)
    legacy_secrets.mkdir(parents=True)

    (new_secrets / "xkiro-api-key.bin").write_bytes(b"new-xkiro")
    (legacy_secrets / "xkiro-api-key.bin").write_bytes(b"legacy-xkiro")
    (legacy_secrets / "google-flow.cookies.bin").write_bytes(b"legacy-flow")

    def fake_user_data_dir(app_name: str, _author: str) -> str:
        return str(tmp_path / app_name)

    monkeypatch.setattr(desktop, "user_data_dir", fake_user_data_dir)
    session = desktop.DesktopSession(SimpleNamespace(FOLDER_DIALOG=20))

    assert session._credential_root == new_secrets.resolve()
    assert (new_secrets / "xkiro-api-key.bin").read_bytes() == b"new-xkiro"
    assert (new_secrets / "google-flow.cookies.bin").read_bytes() == b"legacy-flow"
