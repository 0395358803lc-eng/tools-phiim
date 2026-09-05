"""Cookie vault access, Flow CLI session status and model provisioning."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..flow_credentials import CookieVault, FlowCredentialError, parse_cookie_input
from ..models import FlowConnection
from .catalog import VIDEO_MODELS
from .discovery import _flow_cli_available
from .errors import FlowIntegrationError

logger = logging.getLogger(__name__)


def _parse_cookie_input(value: str) -> tuple[dict[str, str], list[dict[str, object]] | None]:
    try:
        return parse_cookie_input(value)
    except FlowCredentialError as exc:
        raise FlowIntegrationError(str(exc)) from exc


def _browser_ready() -> bool:
    import os

    if not _flow_cli_available():
        return False
    bundled_root = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if bundled_root and any(Path(bundled_root).glob("chromium*/**/chrome.exe")):
        return True
    try:
        from flow_cli.cli.main import _find_playwright_chromium

        return bool(_find_playwright_chromium())
    except Exception:  # noqa: BLE001
        return False


class FlowSession:
    """Owning behavior for the Flow cookie vault and CLI session state.

    Kept as a small stateful object so the browser, download and UI-compat
    concerns stay independently testable.
    """

    def __init__(self, data_root: Path, credential_root: Path | None = None) -> None:
        self.data_root = data_root.resolve()
        credential_dir = (credential_root or self.data_root / "secrets").resolve()
        self.vault = CookieVault(credential_dir / "google-flow.cookies.bin")
        workspace_vault = CookieVault(self.data_root / "secrets" / "google-flow.cookies.bin")
        if workspace_vault.path != self.vault.path and not self.vault.path.exists():
            try:
                cookies, raw = workspace_vault.load()
                if cookies:
                    self._save_cookies(cookies, raw)
            except FlowCredentialError:
                # Keep the app usable so the user can replace a damaged stored Flow CLI session.
                pass

    @property
    def configured(self) -> bool:
        return self.vault.path.is_file()

    def _clear_cookies(self) -> None:
        try:
            self.vault.clear()
        except FlowCredentialError as exc:
            raise FlowIntegrationError(str(exc)) from exc

    def _save_cookies(
        self, cookies: dict[str, str], raw: list[dict[str, object]] | None
    ) -> None:
        try:
            self.vault.save(cookies, raw)
        except FlowCredentialError as exc:
            raise FlowIntegrationError(str(exc)) from exc

    def _load_cookies(self) -> tuple[dict[str, str], list[dict[str, object]] | None]:
        try:
            return self.vault.load()
        except FlowCredentialError as exc:
            raise FlowIntegrationError(str(exc)) from exc

    async def status(self, *, verify: bool = False) -> FlowConnection:
        flow_cli_available = _flow_cli_available()
        stored = self.vault.path.is_file()
        cookies, _ = self._load_cookies() if stored else ({}, None)
        connection = FlowConnection(
            configured=bool(cookies),
            authenticated=False,
            transport="flow-cli" if cookies else "none",
            cookie_count=len(cookies),
            message="Chưa cấu hình Google Flow",
            flow_cli_available=flow_cli_available,
            browser_ready=_browser_ready(),
            models=VIDEO_MODELS,
        )
        if not flow_cli_available:
            connection.message = "Flow CLI tích hợp không khả dụng"
            return connection
        if not cookies:
            connection.message = (
                "Flow CLI đã sẵn sàng; hãy kết nối phiên Google Flow bằng cookie hoặc cookies.json"
            )
            return connection
        if not verify:
            connection.message = "Đã lưu phiên Flow CLI; nhấn Kiểm tra để xác thực"
            return connection

        from flow_cli._auth import validate_cookies

        ok, message, _ = await asyncio.to_thread(validate_cookies, cookies, 15)
        connection.authenticated = ok
        connection.message = message
        if ok:
            try:
                client = self._client(cookies)
                credits = await client.get_credits()
                connection.credits_remaining = credits.remaining
                connection.tier = credits.tier or ""
            except Exception as exc:  # noqa: BLE001
                logger.debug("Unable to load Google Flow credits: %s", exc)
        return connection

    async def connect(self, cookie_input: str) -> FlowConnection:
        if not _flow_cli_available():
            raise FlowIntegrationError("Flow CLI tích hợp chưa khả dụng")
        cookies, raw = _parse_cookie_input(cookie_input)
        from flow_cli._auth import validate_cookies

        ok, message, _ = await asyncio.to_thread(validate_cookies, cookies, 15)
        if not ok:
            raise FlowIntegrationError(message)
        self._save_cookies(cookies, raw)
        return await self.status(verify=True)

    def disconnect(self) -> None:
        self._clear_cookies()

    def _client(self, cookies: dict[str, str], project_id: str | None = None) -> object:
        from .ui_compat import apply_flow_ui_compatibility

        apply_flow_ui_compatibility()
        from flow_cli._client import FlowClient

        client = FlowClient(cookies=cookies, project_id=project_id, timeout=30)
        _, raw = self.vault.load()
        if raw:
            client.raw_cookies = raw
        return client