from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flow_story_studio.browser_sessions import BrowserSessionError, GoogleFlowSessionManager
from flow_story_studio.main import create_app
from flow_story_studio.storage import ProjectStorage


def _manager(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TH_MEDIA_SECRET_KEY", "browser-session-test-secret")
    chrome = tmp_path / "chromium"
    chrome.write_bytes(b"fake")
    launches: list[tuple[Path, Path, int]] = []

    def launch(chrome_path: Path, profile_dir: Path, debug_port: int) -> None:
        launches.append((chrome_path, profile_dir, debug_port))

    return (
        GoogleFlowSessionManager(
            tmp_path / "sessions",
            chrome_path=chrome,
            launcher=launch,
            credential_path=tmp_path / "secrets" / "flow-session.bin",
        ),
        launches,
    )


def test_google_flow_session_import_is_encrypted_and_domain_filtered(
    tmp_path: Path, monkeypatch
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    status = manager.import_session(
        {
            "cookies": [
                {
                    "name": "SID",
                    "value": "google-secret-value",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "OTHER",
                    "value": "must-be-dropped",
                    "domain": ".example.com",
                    "path": "/",
                },
            ],
            "origins": [
                {
                    "origin": "https://flow.google.com",
                    "localStorage": [{"name": "x", "value": "y"}],
                },
                {
                    "origin": "https://example.com",
                    "localStorage": [{"name": "drop", "value": "me"}],
                },
            ],
        }
    )

    assert status["configured"] is False
    assert status["session_present"] is True
    assert status["auth_mode"] == "split_session_import"
    assert status["storage_mode"] == "dual_encrypted_session_vault"
    assert status["google_account_cookie_count"] == 1
    assert status["google_flow_cookie_count"] == 0
    vault = tmp_path / "secrets" / "google-account-session.bin"
    raw = vault.read_bytes()
    assert b"google-secret-value" not in raw
    assert b"must-be-dropped" not in raw


def test_importing_new_account_session_replaces_only_account_vault(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    manager.import_google_account_session(
        {"cookies": [{"name": "SID", "value": "one", "domain": ".google.com"}]}
    )
    manager.import_flow_session(
        {"cookies": [{"name": "OSID", "value": "flow", "domain": "flow.google.com"}]}
    )
    second = manager.import_google_account_session(
        {"cookies": [{"name": "SID", "value": "two", "domain": "accounts.google.com"}]}
    )

    assert second["configured"] is True
    assert second["google_account_cookie_count"] == 1
    assert second["google_flow_cookie_count"] == 1
    merged = manager._load_session()
    sid = next(cookie for cookie in merged["cookies"] if cookie["name"] == "SID")
    assert sid["value"] == "two"
    assert any(cookie["name"] == "OSID" for cookie in merged["cookies"])
    assert manager.active_session_id() == "imported-session"
    assert manager.active_profile_dir().is_dir()


def test_google_flow_session_api_import_and_clear(tmp_path: Path, monkeypatch) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    app = create_app(ProjectStorage(tmp_path / "projects"), browser_session_manager=manager)

    with TestClient(app) as client:
        status = client.get("/api/google-flow/session")
        assert status.status_code == 200
        assert status.json()["configured"] is False

        account = client.post(
            "/api/google-flow/session/google-account/import",
            json={"cookies": [{"name": "SID", "value": "abc", "domain": ".google.com"}]},
        )
        assert account.status_code == 200
        assert account.json()["configured"] is False
        assert account.json()["google_account_cookie_count"] == 1
        assert account.json()["google_flow_cookie_count"] == 0

        flow = client.post(
            "/api/google-flow/session/flow/import",
            json={
                "cookies": [
                    {
                        "name": "OSID",
                        "value": "flow",
                        "domain": "flow.google.com",
                    }
                ]
            },
        )
        assert flow.status_code == 200
        assert flow.json()["configured"] is True
        assert flow.json()["google_account_cookie_count"] == 1
        assert flow.json()["google_flow_cookie_count"] == 1

        cleared = client.delete("/api/google-flow/session")
        assert cleared.status_code == 200
        assert cleared.json()["configured"] is False


def test_legacy_interactive_session_endpoint_is_controlled_error(
    tmp_path: Path, monkeypatch
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    app = create_app(ProjectStorage(tmp_path / "projects"), browser_session_manager=manager)

    with TestClient(app) as client:
        response = client.post("/api/google-flow/session/new")

    assert response.status_code == 409
    assert "Import Session" in response.json()["detail"]


def test_split_import_rejects_cookie_from_wrong_source(tmp_path: Path, monkeypatch) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)

    with pytest.raises(BrowserSessionError, match="Google Account"):
        manager.import_google_account_session(
            {
                "cookies": [
                    {
                        "name": "OSID",
                        "value": "flow-only",
                        "domain": "flow.google.com",
                    }
                ]
            }
        )

    with pytest.raises(BrowserSessionError, match="Google Flow"):
        manager.import_flow_session(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "account-only",
                        "domain": ".google.com",
                    }
                ]
            }
        )


def test_legacy_combined_vault_migrates_into_two_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    manager._flow_vault.save(
        {
            "version": 1,
            "cookies": [
                {
                    "name": "SID",
                    "value": "account",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "OSID",
                    "value": "flow",
                    "domain": "flow.google.com",
                    "path": "/",
                },
            ],
            "origins": [],
        }
    )

    status = manager.status()

    assert status["configured"] is True
    assert status["google_account_cookie_count"] == 1
    assert status["google_flow_cookie_count"] == 1
    assert manager._account_vault.load()["source"] == "google_account"
    assert manager._flow_vault.load()["source"] == "google_flow"


def test_clearing_one_source_keeps_the_other_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    manager.import_google_account_session(
        {"cookies": [{"name": "SID", "value": "account", "domain": ".google.com"}]}
    )
    manager.import_flow_session(
        {"cookies": [{"name": "OSID", "value": "flow", "domain": "flow.google.com"}]}
    )

    status = manager.clear_source_session("google_flow")

    assert status["configured"] is False
    assert status["google_account_configured"] is True
    assert status["google_flow_configured"] is False
    assert status["google_account_cookie_count"] == 1
    assert status["google_flow_cookie_count"] == 0


def test_proxy_config_is_encrypted_and_launch_args_do_not_expose_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)

    status = manager.save_proxy(
        {
            "server": "http://proxy.example:8080",
            "username": "proxy-user",
            "password": "proxy-secret",
        }
    )

    assert status["proxy_configured"] is True
    assert status["proxy_scheme"] == "http"
    assert status["proxy_endpoint"] == "http://proxy.example:8080"
    assert status["proxy_auth_configured"] is True
    assert "proxy-user" not in str(status)
    assert "proxy-secret" not in str(status)

    vault = tmp_path / "secrets" / "google-flow-proxy.bin"
    raw = vault.read_bytes()
    assert b"proxy-user" not in raw
    assert b"proxy-secret" not in raw

    args = manager._chrome_launch_args(
        tmp_path / "chromium",
        tmp_path / "profile",
        9333,
    )
    assert "--proxy-server=http://proxy.example:8080" in args
    assert not any("proxy-user" in item or "proxy-secret" in item for item in args)


def test_proxy_accepts_socks5_with_and_without_auth_and_sock5_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)

    status = manager.save_proxy({"server": "socks5://127.0.0.1:1080"})
    assert status["proxy_configured"] is True
    assert status["proxy_scheme"] == "socks5"
    assert status["proxy_auth_configured"] is False

    status = manager.save_proxy(
        {
            "server": "sock5://proxy.example:1081",
            "username": "user",
            "password": "secret",
        }
    )
    assert status["proxy_configured"] is True
    assert status["proxy_scheme"] == "socks5"
    assert status["proxy_endpoint"] == "socks5://proxy.example:1081"
    assert status["proxy_auth_configured"] is True

    seen = {}

    def fake_bridge(config):
        seen.update(config)
        return "socks5://127.0.0.1:45678"

    monkeypatch.setattr(manager, "_start_socks5_auth_bridge", fake_bridge)
    args = manager._chrome_launch_args(
        tmp_path / "chromium",
        tmp_path / "profile",
        9333,
    )
    assert "--proxy-server=socks5://127.0.0.1:45678" in args
    assert seen["host"] == "proxy.example"
    assert seen["username"] == "user"
    assert seen["password"] == "secret"
    assert not any("proxy.example" in item or "secret" in item for item in args)


def test_proxy_rejects_invalid_scheme_and_partial_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)

    with pytest.raises(BrowserSessionError, match="chỉ hỗ trợ"):
        manager.save_proxy({"server": "ftp://proxy.example:21"})

    with pytest.raises(BrowserSessionError, match="cả username và password"):
        manager.save_proxy(
            {
                "server": "http://proxy.example:8080",
                "username": "user",
            }
        )


def test_proxy_api_save_and_clear(tmp_path: Path, monkeypatch) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    app = create_app(
        ProjectStorage(tmp_path / "projects"),
        browser_session_manager=manager,
    )

    with TestClient(app) as client:
        saved = client.post(
            "/api/google-flow/session/proxy",
            json={
                "server": "https://proxy.example:8443",
                "username": "api-user",
                "password": "api-secret",
            },
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["proxy_configured"] is True
        assert body["proxy_scheme"] == "https"
        assert body["proxy_endpoint"] == "https://proxy.example:8443"
        assert body["proxy_auth_configured"] is True
        assert "api-secret" not in saved.text
        assert "api-user" not in saved.text

        cleared = client.delete("/api/google-flow/session/proxy")
        assert cleared.status_code == 200
        assert cleared.json()["proxy_configured"] is False


class _RuntimeStorageContext:
    def __init__(self, state):
        self._state = state

    def storage_state(self):
        return self._state


class _RuntimeStorageBrowser:
    def __init__(self, state):
        self.contexts = [_RuntimeStorageContext(state)]


def test_runtime_session_persistence_refreshes_both_encrypted_vaults_without_reset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, launches = _manager(tmp_path, monkeypatch)
    manager.import_google_account_session(
        {
            "cookies": [
                {
                    "name": "SID",
                    "value": "account-old",
                    "domain": ".google.com",
                    "path": "/",
                }
            ],
            "origins": [
                {
                    "origin": "https://accounts.google.com",
                    "localStorage": [{"name": "keep", "value": "old"}],
                }
            ],
        }
    )
    manager.import_flow_session(
        {
            "cookies": [
                {
                    "name": "OSID",
                    "value": "flow-old",
                    "domain": "flow.google.com",
                    "path": "/",
                }
            ],
            "origins": [
                {
                    "origin": "https://flow.google.com",
                    "localStorage": [{"name": "flow-old", "value": "1"}],
                }
            ],
        }
    )
    runtime = _RuntimeStorageBrowser(
        {
            "cookies": [
                {
                    "name": "SID",
                    "value": "account-rotated",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "OSID",
                    "value": "flow-rotated",
                    "domain": "flow.google.com",
                    "path": "/",
                    "secure": True,
                },
            ],
            "origins": [
                {
                    "origin": "https://flow.google.com",
                    "localStorage": [{"name": "flow-new", "value": "2"}],
                }
            ],
        }
    )

    status = manager.persist_runtime_session(runtime)

    assert status["runtime_session_persisted"] is True
    assert set(status["runtime_session_sources"]) == {"google_account", "google_flow"}
    assert status["runtime_session_refreshed_at"] > 0
    assert launches == []

    account = manager._account_vault.load()
    flow = manager._flow_vault.load()
    assert account["cookies"][0]["value"] == "account-rotated"
    assert flow["cookies"][0]["value"] == "flow-rotated"
    assert account["refreshed_at"] > 0
    assert flow["refreshed_at"] > 0

    account_local = {
        pair["name"]: pair["value"]
        for origin in account["origins"]
        for pair in origin.get("localStorage", [])
    }
    flow_local = {
        pair["name"]: pair["value"]
        for origin in flow["origins"]
        for pair in origin.get("localStorage", [])
    }
    assert account_local["keep"] == "old"
    assert flow_local["flow-old"] == "1"
    assert flow_local["flow-new"] == "2"


def test_runtime_session_persistence_rejects_non_google_runtime_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _launches = _manager(tmp_path, monkeypatch)
    browser = _RuntimeStorageBrowser(
        {
            "cookies": [
                {
                    "name": "OTHER",
                    "value": "value",
                    "domain": ".example.com",
                    "path": "/",
                }
            ],
            "origins": [],
        }
    )

    with pytest.raises(BrowserSessionError, match="không chứa Google session"):
        manager.persist_runtime_session(browser)
