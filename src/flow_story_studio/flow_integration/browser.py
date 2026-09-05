"""Chrome/CDP attach and browser-driven generation for Flow."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class ExistingChromeAttachError(RuntimeError):
    """Raised only when attaching to an existing Chrome fails before UI actions."""


class _ExistingChromeManager:
    """Attach to the user's consent-enabled Chrome without closing that browser."""

    def __init__(self, port_file: Path) -> None:
        self.port_file = port_file
        self._playwright = None
        self._browser = None
        self._proxy_server = None
        self.context = None

    async def start(self) -> _ExistingChromeManager:
        import websockets
        from playwright.async_api import async_playwright

        lines = self.port_file.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            raise ExistingChromeAttachError("Chrome DevToolsActivePort is incomplete")
        port = lines[0].strip()
        browser_path = lines[1].strip()
        upstream_endpoint = f"ws://127.0.0.1:{port}{browser_path}"

        async def relay(source: object, target: object) -> None:
            try:
                async for message in source:
                    await target.send(message)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    await target.close()
                except Exception:  # noqa: BLE001
                    pass

        async def proxy_handler(client: object) -> None:
            async with websockets.connect(
                upstream_endpoint,
                open_timeout=30,
                max_size=None,
            ) as upstream:
                await asyncio.gather(
                    relay(client, upstream),
                    relay(upstream, client),
                )

        try:
            self._proxy_server = await websockets.serve(
                proxy_handler,
                "127.0.0.1",
                0,
                max_size=None,
            )
            proxy_port = self._proxy_server.sockets[0].getsockname()[1]
            proxy_endpoint = f"ws://127.0.0.1:{proxy_port}"
            self._playwright = await async_playwright().start()
            self._browser = await asyncio.wait_for(
                self._playwright.chromium.connect_over_cdp(proxy_endpoint),
                timeout=15.0,
            )
            if not self._browser.contexts:
                raise ExistingChromeAttachError(
                    "Chrome remote debugging exposed no browser context"
                )
            self.context = self._browser.contexts[0]
            return self
        except ExistingChromeAttachError:
            await self.stop()
            raise
        except Exception as exc:  # noqa: BLE001
            await self.stop()
            raise ExistingChromeAttachError(
                "Could not attach Playwright through the raw-CDP compatibility "
                f"proxy: {type(exc).__name__}"
            ) from exc

    async def stop(self) -> None:
        # Never close the real Chrome/browser context. Stopping Playwright only
        # disconnects this automation client from the consent-enabled endpoint.
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Playwright disconnect cleanup failed: %s", exc)
        if self._proxy_server is not None:
            try:
                self._proxy_server.close()
                await self._proxy_server.wait_closed()
            except Exception as exc:  # noqa: BLE001
                logger.debug("CDP compatibility proxy cleanup failed: %s", exc)
        self._playwright = None
        self._browser = None
        self._proxy_server = None
        self.context = None

    async def __aenter__(self) -> _ExistingChromeManager:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()


def is_flow_cookie_domain(domain: str) -> bool:
    normalized = domain.strip().lower().lstrip(".")
    return (
        normalized == "google.com"
        or normalized.endswith(".google.com")
        or normalized == "labs.google"
        or normalized.endswith(".labs.google")
    )


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
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "flow.google.com":
        raise ValueError("Flow Chrome launcher only accepts https://flow.google.com URLs")
    profile = profile_root.resolve()
    port_file = profile / "DevToolsActivePort"
    if can_attach_existing_chrome(port_file):
        return port_file
    chrome = find_chrome_executable()
    if chrome is None:
        raise RuntimeError("Google Chrome is not installed")
    profile.mkdir(parents=True, exist_ok=True)
    # Chrome executable comes only from fixed install roots; argv is validated; shell is never used.
    subprocess.Popen(  # nosec B603
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


async def _export_flow_cookies_from_context(
    context: Any,
) -> tuple[dict[str, str], list[dict[str, object]]]:
    browser_cookies = await context.cookies()
    raw: list[dict[str, object]] = []
    cookies: dict[str, str] = {}
    for item in browser_cookies:
        domain = str(item.get("domain") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name or not value or not is_flow_cookie_domain(domain):
            continue
        cookies[name] = value
        exported: dict[str, object] = {
            "name": name,
            "value": value,
            "domain": str(item.get("domain") or ""),
            "path": str(item.get("path") or "/"),
            "httpOnly": bool(item.get("httpOnly", False)),
            "secure": bool(item.get("secure", False)),
        }
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            exported["expirationDate"] = float(expires)
        same_site = str(item.get("sameSite") or "")
        if same_site:
            exported["sameSite"] = same_site
        raw.append(exported)
    return cookies, raw


async def export_flow_cookies(
    port_file: Path,
) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Read Google cookies from the consent-enabled Flow Chrome profile."""
    async with _ExistingChromeManager(port_file) as manager:
        if manager.context is None:
            raise RuntimeError("Chrome Flow profile has no browser context")
        return await _export_flow_cookies_from_context(manager.context)


def _project_id_from_flow_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "flow.google.com":
        return None
    if "/project/" not in parsed.path:
        return None
    project_id = parsed.path.split("/project/", 1)[1].split("/", 1)[0].strip()
    return project_id or None


async def _probe_live_flow_context(context: Any) -> tuple[bool, str | None]:
    project_ids: list[str] = []
    for page in context.pages:
        project_id = _project_id_from_flow_url(page.url)
        if project_id:
            project_ids.append(project_id)
        parsed = urlparse(page.url)
        if parsed.hostname != "flow.google.com":
            continue
        try:
            editor = page.locator(
                'div.ProseMirror[contenteditable="true"]'
            ).first
            account = page.locator('button[aria-label="Account details"]').first
            new_project = page.get_by_role("button", name="New project", exact=True).first
            if (
                await editor.is_visible(timeout=250)
                or await account.is_visible(timeout=250)
                or await new_project.is_visible(timeout=250)
            ):
                unique = list(dict.fromkeys(project_ids))
                return True, unique[0] if len(unique) == 1 else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow live-session page probe failed: %s", exc)
    return False, None


async def inspect_live_flow_session(
    port_file: Path,
) -> tuple[
    dict[str, str],
    list[dict[str, object]],
    bool,
    str | None,
]:
    """Read cookies and verify Flow UI with one Chrome CDP connection."""
    async with _ExistingChromeManager(port_file) as manager:
        if manager.context is None:
            raise RuntimeError("Chrome Flow profile has no browser context")
        cookies, raw = await _export_flow_cookies_from_context(manager.context)
        authenticated, project_id = await _probe_live_flow_context(manager.context)
        return cookies, raw, authenticated, project_id


async def probe_live_flow_session(port_file: Path) -> tuple[bool, str | None]:
    """Verify authentication against the live Flow UI, not the retired labs.google API."""
    async with _ExistingChromeManager(port_file) as manager:
        if manager.context is None:
            return False, None
        return await _probe_live_flow_context(manager.context)


async def _ensure_live_flow_project_context(context: Any) -> str:
    ids = [
        project_id
        for page in context.pages
        if (project_id := _project_id_from_flow_url(page.url))
    ]
    unique = list(dict.fromkeys(ids))
    if len(unique) == 1:
        return unique[0]

    page = await context.new_page()
    try:
        await page.goto(
            "https://flow.google.com/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(1200)
        button = page.get_by_role("button", name="New project", exact=True).first
        if not await button.is_visible(timeout=5_000):
            raise RuntimeError(
                "Flow home is open but the New project control is unavailable"
            )
        await button.click(timeout=5_000)
        for _ in range(60):
            await page.wait_for_timeout(250)
            project_id = _project_id_from_flow_url(page.url)
            if project_id:
                return project_id
        raise RuntimeError("Flow did not navigate to a new project after New project")
    finally:
        await page.close()


async def ensure_live_flow_project(
    port_file: Path,
    *,
    manager: _ExistingChromeManager | None = None,
) -> str:
    """Return an unambiguous live project or create one through Flow's current UI."""
    if manager is not None:
        if manager.context is None:
            raise RuntimeError("Chrome Flow profile has no browser context")
        return await _ensure_live_flow_project_context(manager.context)
    async with _ExistingChromeManager(port_file) as owned:
        if owned.context is None:
            raise RuntimeError("Chrome Flow profile has no browser context")
        return await _ensure_live_flow_project_context(owned.context)
