import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from flow_story_studio.flow_credentials import CookieVault
from flow_story_studio.flow_integration import FlowIntegrationError
from flow_story_studio.flow_integration import session as session_module
from flow_story_studio.flow_integration.browser import (
    _ExistingChromeManager,
    can_attach_existing_chrome,
)
from flow_story_studio.flow_integration.session import FlowSession
from flow_story_studio.models import FlowConnection


@pytest.mark.asyncio
async def test_status_reports_flow_cli_unavailable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_module, "_flow_cli_available", lambda: False)
    monkeypatch.setattr(session_module, "_browser_ready", lambda: False)
    session = FlowSession(tmp_path)

    status = await session.status(verify=True)

    assert status.configured is False
    assert status.flow_cli_available is False
    assert status.browser_ready is False
    assert "không khả dụng" in status.message


@pytest.mark.asyncio
async def test_status_reports_saved_unverified_session(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_module, "_flow_cli_available", lambda: True)
    monkeypatch.setattr(session_module, "_browser_ready", lambda: True)
    session = FlowSession(tmp_path)
    session._save_cookies({"SID": "saved"}, None)

    status = await session.status(verify=False)

    assert status.configured is True
    assert status.authenticated is False
    assert status.transport == "flow-cli"
    assert status.cookie_count == 1
    assert status.browser_ready is True


@pytest.mark.asyncio
async def test_status_verify_rejects_expired_cookie(monkeypatch, tmp_path: Path) -> None:
    from flow_cli import _auth

    monkeypatch.setattr(session_module, "_flow_cli_available", lambda: True)
    monkeypatch.setattr(session_module, "_browser_ready", lambda: True)
    monkeypatch.setattr(
        _auth,
        "validate_cookies",
        lambda _cookies, _timeout: (False, "expired", None),
    )
    session = FlowSession(tmp_path)
    session._save_cookies({"SID": "expired"}, None)

    status = await session.status(verify=True)

    assert status.configured is True
    assert status.authenticated is False
    assert status.message == "expired"


@pytest.mark.asyncio
async def test_status_verify_reads_credits_without_generation(
    monkeypatch, tmp_path: Path
) -> None:
    from flow_cli import _auth

    monkeypatch.setattr(session_module, "_flow_cli_available", lambda: True)
    monkeypatch.setattr(session_module, "_browser_ready", lambda: True)
    monkeypatch.setattr(
        _auth,
        "validate_cookies",
        lambda _cookies, _timeout: (True, "ok", None),
    )
    session = FlowSession(tmp_path)
    session._save_cookies({"SID": "valid"}, None)

    class FakeClient:
        async def get_credits(self):
            return SimpleNamespace(remaining=123, tier="test-tier")

    monkeypatch.setattr(session, "_client", lambda _cookies: FakeClient())

    status = await session.status(verify=True)

    assert status.authenticated is True
    assert status.credits_remaining == 123
    assert status.tier == "test-tier"


@pytest.mark.asyncio
async def test_connect_rejects_invalid_cookie_without_persisting(
    monkeypatch, tmp_path: Path
) -> None:
    from flow_cli import _auth

    monkeypatch.setattr(session_module, "_flow_cli_available", lambda: True)
    monkeypatch.setattr(
        _auth,
        "validate_cookies",
        lambda _cookies, _timeout: (False, "invalid session", None),
    )
    session = FlowSession(tmp_path)

    with pytest.raises(FlowIntegrationError, match="invalid session"):
        await session.connect("SID=value; HSID=value2")

    assert session.configured is False


@pytest.mark.asyncio
async def test_connect_persists_only_after_validation(monkeypatch, tmp_path: Path) -> None:
    from flow_cli import _auth

    monkeypatch.setattr(session_module, "_flow_cli_available", lambda: True)
    monkeypatch.setattr(
        _auth,
        "validate_cookies",
        lambda _cookies, _timeout: (True, "ok", None),
    )
    session = FlowSession(tmp_path)

    async def fake_status(*, verify: bool = False):
        assert verify is True
        return FlowConnection(
            configured=True,
            authenticated=True,
            transport="flow-cli",
            cookie_count=2,
            message="ok",
            flow_cli_available=True,
        )

    monkeypatch.setattr(session, "status", fake_status)
    status = await session.connect("SID=value; HSID=value2")

    assert status.authenticated is True
    assert session.configured is True
    cookies, _raw = session._load_cookies()
    assert cookies == {"SID": "value", "HSID": "value2"}


def test_session_migrates_workspace_cookie_vault(tmp_path: Path) -> None:
    data_root = tmp_path / "workspace"
    credential_root = tmp_path / "user-vault"
    workspace_vault = CookieVault(data_root / "secrets" / "google-flow.cookies.bin")
    workspace_vault.save({"SID": "legacy-workspace"}, None)

    session = FlowSession(data_root, credential_root=credential_root)

    assert session.configured is True
    cookies, _raw = session._load_cookies()
    assert cookies == {"SID": "legacy-workspace"}


def test_can_attach_existing_chrome_with_live_local_port(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        port_file = tmp_path / "DevToolsActivePort"
        port_file.write_text(
            f"{port}\n/devtools/browser/test\n",
            encoding="utf-8",
        )
        assert can_attach_existing_chrome(port_file) is True
    finally:
        listener.close()


@pytest.mark.asyncio
async def test_existing_chrome_manager_rejects_incomplete_port_file(tmp_path: Path) -> None:
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n", encoding="utf-8")
    manager = _ExistingChromeManager(port_file)

    with pytest.raises(RuntimeError, match="incomplete"):
        await manager.start()


def test_browser_ready_uses_bundled_playwright_chromium(monkeypatch, tmp_path: Path) -> None:
    browser = tmp_path / "chromium-123" / "chrome-win" / "chrome.exe"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"chrome")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(session_module, "_flow_cli_available", lambda: True)

    assert session_module._browser_ready() is True


@pytest.mark.asyncio
async def test_existing_chrome_manager_attaches_and_disconnects_cleanly(
    monkeypatch, tmp_path: Path
) -> None:
    from playwright import async_api

    context = object()
    calls: list[str] = []

    class FakeBrowser:
        contexts = [context]

    class FakeChromium:
        async def connect_over_cdp(self, endpoint: str):
            calls.append(endpoint)
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self) -> None:
            calls.append("stop")

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    monkeypatch.setattr(async_api, "async_playwright", lambda: FakeStarter())
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/session\n", encoding="utf-8")

    manager = _ExistingChromeManager(port_file)
    started = await manager.start()

    assert started is manager
    assert manager.context is context
    assert calls[0].startswith("ws://127.0.0.1:")
    assert "/devtools/browser/session" not in calls[0]
    await manager.stop()
    assert calls[-1] == "stop"
    assert manager.context is None


@pytest.mark.asyncio
async def test_existing_chrome_manager_fails_closed_when_no_contexts(
    monkeypatch, tmp_path: Path
) -> None:
    from playwright import async_api

    stopped: list[bool] = []

    class FakeBrowser:
        contexts: list[object] = []

    class FakeChromium:
        async def connect_over_cdp(self, _endpoint: str):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self) -> None:
            stopped.append(True)

    class FakeStarter:
        async def start(self):
            return FakePlaywright()

    monkeypatch.setattr(async_api, "async_playwright", lambda: FakeStarter())
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/session\n", encoding="utf-8")

    manager = _ExistingChromeManager(port_file)
    with pytest.raises(RuntimeError, match="no browser context"):
        await manager.start()

    assert stopped == [True]
    assert manager.context is None
