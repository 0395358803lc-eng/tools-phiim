"""Chrome/CDP attach and browser-driven generation for Flow."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _ExistingChromeManager:
    """Attach to the user's consent-enabled Chrome without closing that browser."""

    def __init__(self, port_file: Path) -> None:
        self.port_file = port_file
        self._playwright = None
        self._browser = None
        self.context = None

    async def start(self) -> _ExistingChromeManager:
        from playwright.async_api import async_playwright

        lines = self.port_file.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            raise RuntimeError("Chrome DevToolsActivePort is incomplete")
        ws_endpoint = f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(ws_endpoint)
        if not self._browser.contexts:
            await self.stop()
            raise RuntimeError("Chrome remote debugging exposed no browser context")
        self.context = self._browser.contexts[0]
        return self

    async def stop(self) -> None:
        # Never close the real Chrome/browser context. Stopping Playwright only
        # disconnects this automation client from the consent-enabled endpoint.
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Playwright disconnect cleanup failed: %s", exc)
        self._playwright = None
        self._browser = None
        self.context = None

    async def __aenter__(self) -> _ExistingChromeManager:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()


def can_attach_existing_chrome(port_file: Path) -> bool:
    if not port_file.is_file():
        return False
    try:
        lines = port_file.read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except (OSError, ValueError, IndexError):
        return False

def default_flow_chrome_profile() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    base = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    return (base / "TH Media" / "Flow Chrome").resolve()


def find_chrome_executable() -> Path | None:
    candidates = [
        Path(os.getenv("PROGRAMFILES", r"C:\Program Files"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ]
    return next((item.resolve() for item in candidates if item.is_file()), None)


def launch_flow_chrome(
    profile_root: Path,
    *,
    url: str = "https://flow.google.com/",
    timeout: float = 8.0,
) -> Path:
    """Launch a dedicated consent-enabled Chrome profile and return its CDP port file."""
    profile = profile_root.resolve()
    port_file = profile / "DevToolsActivePort"
    if can_attach_existing_chrome(port_file):
        return port_file
    chrome = find_chrome_executable()
    if chrome is None:
        raise RuntimeError("Google Chrome is not installed")
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(  # noqa: S603
        [
            str(chrome),
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if can_attach_existing_chrome(port_file):
            return port_file
        time.sleep(0.1)
    raise RuntimeError(
        "Chrome Flow session started but the DevTools endpoint did not become ready"
    )
