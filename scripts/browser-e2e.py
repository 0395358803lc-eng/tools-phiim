"""Headless browser E2E smoke for the offline project-analysis workflow."""

from __future__ import annotations

import socket
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from flow_story_studio.main import create_app
from flow_story_studio.storage import ProjectStorage

TEXT = (
    "Người đàn ông bước vào văn phòng và đặt điện thoại lên bàn. "
    "Anh nhìn đồng hồ rồi đi tới cửa sổ. Sau đó anh quay lại ghế và bắt đầu nói."
)


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_ready(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Browser E2E backend did not become ready")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="flow-story-browser-e2e-") as temp:
        root = Path(temp)
        app = create_app(ProjectStorage(root / "projects"), credential_root=root / "secrets")
        port = available_port()
        base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=server.run, name="browser-e2e-server", daemon=True)
        thread.start()
        try:
            wait_ready(base_url)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(base_url, wait_until="domcontentloaded")
                if not page.title().startswith("TH Media"):
                    raise AssertionError(f"Unexpected application title: {page.title()}")
                if page.locator(".brand strong").inner_text().strip() != "TH MEDIA":
                    raise AssertionError("TH Media brand name is missing from the topbar")
                if page.locator(".brand small").inner_text().strip() != "AI STORY PRODUCTION":
                    raise AssertionError("TH Media brand tagline is missing from the topbar")
                page.locator("#projectNameInput").fill("Production E2E")
                page.locator("#storyInput").fill(TEXT)
                page.locator("#analysisProviderInput").select_option("offline")
                page.locator("#providerInput").evaluate("function(el) { el.value = 'mock'; }")
                page.locator("#analyzeSubmit").click()
                page.locator("#projectTitle").wait_for(state="visible", timeout=30_000)
                page.wait_for_function(
                    "document.querySelector('#projectTitle').textContent === 'Production E2E'",
                    timeout=90_000,
                )
                scene_count = int(page.locator("#sceneCount").inner_text())
                if scene_count < 1:
                    raise AssertionError("Browser E2E produced no scenes")
                if page.locator("#sourcePreview").inner_text().strip() != TEXT:
                    raise AssertionError("Browser E2E did not hydrate the full project payload")
                browser.close()
                print(f"Browser E2E passed with {scene_count} scene(s)")
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
