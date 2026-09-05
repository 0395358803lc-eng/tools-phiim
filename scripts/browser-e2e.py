"""Headless browser E2E smoke for the offline project-analysis workflow."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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


def read_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_analysis_job(base_url: str, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = read_json(f"{base_url}/api/analysis/jobs/{job_id}")
        if latest.get("status") in {"completed", "failed", "cancelled"}:
            return latest
        time.sleep(0.1)
    return latest


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
                browser_messages: list[str] = []
                page.on(
                    "console",
                    lambda message: browser_messages.append(
                        f"console:{message.type}:{message.text}"
                    ),
                )
                page.on(
                    "pageerror",
                    lambda error: browser_messages.append(f"pageerror:{error}"),
                )
                page.on(
                    "requestfailed",
                    lambda request: browser_messages.append(
                        f"requestfailed:{request.method}:{request.url}:{request.failure}"
                    ),
                )
                page.locator("#projectNameInput").fill("Production E2E")
                page.locator("#storyInput").fill(TEXT)
                page.locator("#analysisProviderInput").select_option("offline")
                page.locator("#providerInput").evaluate("function(el) { el.value = 'mock'; }")
                if not page.locator("#newProjectForm").evaluate("(el) => el.reportValidity()"):
                    raise AssertionError("Browser E2E analysis form is unexpectedly invalid")
                with page.expect_response(
                    lambda response: (
                        "/api/analysis/jobs?" in response.url
                        and response.request.method == "POST"
                    ),
                    timeout=10_000,
                ) as response_info:
                    page.locator("#analyzeSubmit").click()
                started = response_info.value
                if started.status != 202:
                    raise AssertionError(
                        f"Analysis POST failed with HTTP {started.status}: {started.text()}"
                    )
                job_id = str(started.json()["id"])
                backend_job = wait_analysis_job(base_url, job_id)
                if backend_job.get("status") != "completed":
                    raise AssertionError(
                        "Backend analysis did not complete: "
                        + json.dumps(backend_job, ensure_ascii=False)
                    )
                page.locator("#projectTitle").wait_for(state="visible", timeout=30_000)
                try:
                    page.wait_for_function(
                        "document.querySelector('#projectTitle').textContent === 'Production E2E'",
                        timeout=15_000,
                    )
                except PlaywrightTimeoutError as exc:
                    diagnostics = {
                        "backend_job": backend_job,
                        "analysis_status": page.locator("#analysisJobStatus").inner_text(),
                        "analysis_log": page.locator("#analysisLogEntries").inner_text(),
                        "toast": page.locator("#toast").inner_text(),
                        "browser_messages": browser_messages,
                    }
                    raise AssertionError(
                        "Backend completed but UI did not hydrate: "
                        + json.dumps(diagnostics, ensure_ascii=False)
                    ) from exc
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
