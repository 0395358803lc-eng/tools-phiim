from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess  # nosec B404
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.sync_api import (
    Browser,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)

from .browser_sessions import BrowserSessionError, GoogleFlowSessionManager
from .film.subclip_planner import FlowSubclip, plan_flow_subclips
from .media_tools import ffmpeg_path
from .models import Project, Scene

FLOW_HOME_URL = "https://flow.google.com/"
FLOW_PROJECT_URL = "https://flow.google.com/project/{project_id}"
FLOW_PROJECT_RE = re.compile(r"/project/([0-9a-f-]{16,})", re.IGNORECASE)

FLOW_VIDEO_MODELS = (
    "Omni 1.1 Flash",
    "Veo 3.1 - Lite",
    "Veo 3.1 - Fast",
    "Veo 3.1 - Quality",
    "Veo 3.1 - Lite [Lower Priority]",
)
FLOW_IMAGE_MODELS = (
    "Nano Banana Pro",
    "Nano Banana 2",
    "Nano Banana 2 Lite",
)
FLOW_VIDEO_DURATIONS = (4, 6, 8, 10)
FLOW_VIDEO_ASPECTS = ("16:9", "9:16")
FLOW_VIDEO_RESOLUTIONS = ("360p", "720p")
FLOW_IMAGE_ASPECTS = ("16:9", "4:3", "1:1", "3:4", "9:16")


class GoogleFlowBrowserError(RuntimeError):
    pass


class GoogleFlowBrowserTimeout(GoogleFlowBrowserError):
    pass


@dataclass(frozen=True, slots=True)
class FlowGenerationFailure:
    code: str
    detail: str
    retryable: bool
    prompt_repairable: bool = False


def classify_flow_generation_failure(text: str) -> FlowGenerationFailure:
    """Classify visible Google Flow failure text into deterministic app behavior."""
    detail = " ".join(str(text or "").split())[:700]
    folded = detail.casefold()
    if "unusual activity" in folded:
        return FlowGenerationFailure(
            code="account_guard",
            detail=(
                "Google Flow chặn generation do unusual activity. "
                "Dừng retry tự động để tránh tạo vòng lặp/anti-abuse."
            ),
            retryable=False,
        )
    if "credit" in folded and any(
        token in folded for token in ("low", "insufficient", "not enough", "out of")
    ):
        return FlowGenerationFailure(
            code="credits",
            detail="Google Flow không đủ AI credits cho generation hiện tại.",
            retryable=False,
        )
    if any(token in folded for token in ("sign in", "login", "authentication", "session expired")):
        return FlowGenerationFailure(
            code="auth",
            detail="Phiên Google Flow không còn xác thực; cần đăng nhập lại.",
            retryable=False,
        )
    if "the agent failed" in folded and "try again" in folded:
        return FlowGenerationFailure(
            code="agent_failed",
            detail="Google Flow agent thất bại tạm thời và yêu cầu thử lại.",
            retryable=True,
        )
    if any(
        token in folded
        for token in (
            "policy",
            "safety",
            "content is not allowed",
            "couldn't generate",
            "could not generate",
            "prompt",
        )
    ):
        return FlowGenerationFailure(
            code="prompt_rejected",
            detail=(
                "Google Flow từ chối generation liên quan đến nội dung/prompt. "
                "Không retry nguyên prompt; cần dùng prompt đã được chuẩn hóa theo source truth."
            ),
            retryable=False,
            prompt_repairable=True,
        )
    return FlowGenerationFailure(
        code="provider_failed",
        detail=detail or "Google Flow generation thất bại.",
        retryable=False,
    )


@dataclass(slots=True)
class FlowAsset:
    project_id: str
    media_id: str
    label: str
    kind: str
    result_file: str
    source_url: str = ""


@dataclass(slots=True)
class FlowVideoSettings:
    model: str
    aspect_ratio: str
    resolution: str
    duration: int
    outputs: int = 1


class GoogleFlowBrowserWorker:
    """Drive Google Flow through Chromium using an encrypted imported session.

    The worker still uses the public Google Flow browser UI. Authentication state is
    supplied by GoogleFlowSessionManager and never returned through render APIs.
    """

    def __init__(
        self,
        sessions: GoogleFlowSessionManager,
        data_root: Path,
        *,
        debug_port: int = 9333,
        generation_timeout_seconds: float | None = None,
    ) -> None:
        self.sessions = sessions
        self.data_root = data_root.resolve()
        self.debug_port = debug_port
        self.external_cdp_url = os.getenv("TH_MEDIA_FLOW_CDP_URL", "").strip()
        self.external_cdp_apply_imported_session = os.getenv(
            "TH_MEDIA_FLOW_CDP_APPLY_IMPORTED_SESSION", ""
        ).strip().lower() in {"1", "true", "yes"}
        configured_timeout = os.getenv("TH_MEDIA_FLOW_TIMEOUT_SECONDS", "").strip()
        self.generation_timeout_seconds = (
            generation_timeout_seconds
            if generation_timeout_seconds is not None
            else float(configured_timeout or 900)
        )
        configured_guard_cooldown = os.getenv(
            "TH_MEDIA_FLOW_ACCOUNT_GUARD_COOLDOWN_SECONDS", ""
        ).strip()
        self.account_guard_cooldown_seconds = max(
            30.0,
            float(configured_guard_cooldown or 600),
        )
        configured_generation_interval = os.getenv(
            "TH_MEDIA_FLOW_MIN_GENERATION_INTERVAL_SECONDS", ""
        ).strip()
        self.min_generation_interval_seconds = max(
            0.0,
            float(configured_generation_interval or 90),
        )
        self._generation_blocked_until = 0.0
        self._generation_block_reason = ""
        self.worker_root = self.data_root / "browser-worker"
        self.worker_root.mkdir(parents=True, exist_ok=True)
        self._generation_circuit_path = self.worker_root / "generation-circuit.json"
        self._generation_pacing_path = self.worker_root / "generation-pacing.json"
        self._generation_pacing_lock_path = self.worker_root / "generation-pacing.lock"
        self.staging_root = self.worker_root / "staging"
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def configured(self) -> bool:
        if self.external_cdp_url:
            return True
        status = self.sessions.status()
        return bool(status.get("configured") and status.get("chrome_available"))

    @staticmethod
    def _valid_cdp_url(value: str) -> bool:
        if not value:
            return False
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https", "ws", "wss"} and bool(parsed.hostname)

    def _browser_mode(self) -> str:
        return "external_cdp" if self.external_cdp_url else "managed_server_chromium"

    def _read_shared_generation_circuit(self) -> tuple[float, str]:
        path = getattr(self, "_generation_circuit_path", None)
        if path is None or not path.is_file():
            return 0.0, ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            blocked_until = float(payload.get("blocked_until_epoch") or 0.0)
            reason = str(payload.get("reason") or "")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0.0, ""
        if blocked_until <= time.time():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return 0.0, ""
        return blocked_until, reason

    def _write_shared_generation_circuit(self, blocked_until: float, reason: str) -> None:
        path = getattr(self, "_generation_circuit_path", None)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {
            "version": 1,
            "blocked_until_epoch": blocked_until,
            "reason": reason,
            "updated_at_epoch": time.time(),
        }
        try:
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _cooldown_remaining_seconds(self) -> int:
        local_remaining = max(
            0,
            int(getattr(self, "_generation_blocked_until", 0.0) - time.monotonic()),
        )
        shared_until, shared_reason = self._read_shared_generation_circuit()
        shared_remaining = max(0, int(shared_until - time.time()))
        if shared_remaining > local_remaining:
            self._generation_block_reason = shared_reason
        return max(local_remaining, shared_remaining)

    def _assert_generation_allowed(self) -> None:
        remaining = self._cooldown_remaining_seconds()
        if remaining <= 0:
            self._generation_blocked_until = 0.0
            self._generation_block_reason = ""
            return
        raise GoogleFlowBrowserError(
            "[provider_cooldown] Google Flow đang tạm khóa generation sau account guard; "
            f"còn khoảng {remaining}s. Không gửi thêm request tự động trong thời gian này."
        )

    def _trip_generation_circuit(self, failure: FlowGenerationFailure) -> None:
        if failure.code != "account_guard":
            return
        duration = self.account_guard_cooldown_seconds
        self._generation_blocked_until = max(
            getattr(self, "_generation_blocked_until", 0.0),
            time.monotonic() + duration,
        )
        self._generation_block_reason = failure.code

        shared_until, _shared_reason = self._read_shared_generation_circuit()
        blocked_until = max(shared_until, time.time() + duration)
        self._write_shared_generation_circuit(blocked_until, failure.code)

    def _read_generation_pacing_state(self) -> dict[str, float]:
        path = getattr(self, "_generation_pacing_path", None)
        if path is None or not path.is_file():
            return {
                "last_generation_start_epoch": 0.0,
                "last_generation_finish_epoch": 0.0,
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {
                "last_generation_start_epoch": max(
                    0.0,
                    float(payload.get("last_generation_start_epoch") or 0.0),
                ),
                "last_generation_finish_epoch": max(
                    0.0,
                    float(payload.get("last_generation_finish_epoch") or 0.0),
                ),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {
                "last_generation_start_epoch": 0.0,
                "last_generation_finish_epoch": 0.0,
            }

    def _write_generation_pacing_state(self, **updates: float) -> None:
        path = getattr(self, "_generation_pacing_path", None)
        if path is None:
            return
        state = self._read_generation_pacing_state()
        for key, value in updates.items():
            if key in state:
                state[key] = max(0.0, float(value))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {
            "version": 2,
            **state,
            "updated_at_epoch": time.time(),
        }
        try:
            tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_last_generation_start_epoch(self) -> float:
        return self._read_generation_pacing_state()["last_generation_start_epoch"]

    def _read_last_generation_finish_epoch(self) -> float:
        return self._read_generation_pacing_state()["last_generation_finish_epoch"]

    def _write_last_generation_start_epoch(self, started_at: float) -> None:
        self._write_generation_pacing_state(last_generation_start_epoch=started_at)

    def _mark_generation_finished(self) -> None:
        self._write_generation_pacing_state(last_generation_finish_epoch=time.time())

    def _generation_pacing_remaining_seconds(self) -> int:
        interval = max(
            0.0,
            float(getattr(self, "min_generation_interval_seconds", 0.0)),
        )
        if interval <= 0:
            return 0
        state = self._read_generation_pacing_state()
        last_start = state["last_generation_start_epoch"]
        last_finish = state["last_generation_finish_epoch"]
        # Once a generation has finished, rest for the full interval from completion.
        # While a generation is still active (start > finish), retain the old start-based
        # guard so a second process cannot immediately submit another request.
        anchor = last_finish if last_finish >= last_start else last_start
        if anchor <= 0:
            return 0
        return max(0, math.ceil(anchor + interval - time.time()))

    def _acquire_generation_pacing_lock(self, timeout_seconds: float = 30.0) -> int:
        path = getattr(self, "_generation_pacing_lock_path", None)
        if path is None:
            return -1
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
                return fd
            except FileExistsError:
                try:
                    age = time.time() - path.stat().st_mtime
                    if age > 120.0:
                        path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise GoogleFlowBrowserError(
                        "[provider_busy] Không lấy được shared generation pacing lock"
                    ) from None
                time.sleep(0.1)

    def _release_generation_pacing_lock(self, fd: int) -> None:
        path = getattr(self, "_generation_pacing_lock_path", None)
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _wait_for_generation_slot(self) -> None:
        self._assert_generation_allowed()
        interval = max(0.0, float(getattr(self, "min_generation_interval_seconds", 0.0)))
        if interval <= 0:
            return
        fd = self._acquire_generation_pacing_lock()
        try:
            while True:
                self._assert_generation_allowed()
                remaining = self._generation_pacing_remaining_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, float(remaining)))
            self._assert_generation_allowed()
            self._write_last_generation_start_epoch(time.time())
        finally:
            self._release_generation_pacing_lock(fd)

    def health(self) -> dict[str, object]:
        browser_mode = self._browser_mode()
        configured = self.configured()
        ready = False
        if browser_mode == "external_cdp":
            ready = self._valid_cdp_url(self.external_cdp_url)
        elif configured:
            try:
                ready = self.sessions.automation_browser_ready(self.debug_port)
            except BrowserSessionError:
                ready = False
        return {
            "ok": configured,
            "configured": configured,
            "provider": "google-flow-browser",
            "browser_mode": browser_mode,
            "trusted_browser_transport": browser_mode == "external_cdp",
            "automation_ready": ready,
            "debug_port": self.debug_port if browser_mode != "external_cdp" else None,
            "generation_cooldown_seconds": self._cooldown_remaining_seconds(),
            "generation_cooldown_reason": self._generation_block_reason,
            "generation_min_interval_seconds": self.min_generation_interval_seconds,
            "generation_pacing_remaining_seconds": self._generation_pacing_remaining_seconds(),
            "models": list(FLOW_VIDEO_MODELS),
            "image_models": list(FLOW_IMAGE_MODELS),
            "capabilities": {
                "video_aspect_ratios": list(FLOW_VIDEO_ASPECTS),
                "video_resolutions": list(FLOW_VIDEO_RESOLUTIONS),
                "video_durations": list(FLOW_VIDEO_DURATIONS),
                "video_outputs": [1, 2, 3, 4],
                "video_input_modes": ["Frames", "Ingredients"],
                "image_aspect_ratios": list(FLOW_IMAGE_ASPECTS),
                "image_outputs": [1, 2, 3, 4],
                "max_single_clip_seconds": max(FLOW_VIDEO_DURATIONS),
            },
            "message": (
                "Trusted external Chrome transport đã cấu hình."
                if browser_mode == "external_cdp" and configured
                else (
                    "Google Flow browser session sẵn sàng."
                    if configured
                    else "Chưa có Google Flow session active hoặc không tìm thấy Chrome."
                )
            ),
        }

    @staticmethod
    def _normalize(page: Page) -> None:
        for _ in range(4):
            page.keyboard.press("Escape")
            page.wait_for_timeout(80)
        backdrop = page.locator(".cdk-overlay-backdrop:visible")
        if backdrop.count():
            try:
                backdrop.last.click(force=True, timeout=800)
            except PlaywrightError:
                page.keyboard.press("Escape")
        page.wait_for_timeout(100)

    @staticmethod
    def _visible_radio(page: Page, label: str) -> Locator:
        return page.locator('[role="radio"]:visible').filter(has_text=label)

    @staticmethod
    def _visible_button(page: Page, *, aria: str) -> Locator:
        return page.locator(f'button[aria-label="{aria}"]:visible')

    @staticmethod
    def _tile_by_label(page: Page, label: str) -> Locator:
        return page.locator("flow-grid-tile-container").filter(
            has=page.locator(f'[aria-label="{label}"]')
        )

    @staticmethod
    def _tile_exact_label(page: Page, label: str) -> Locator:
        # CSS attribute selectors are safe here because staged labels contain only
        # filesystem-safe characters generated by this worker.
        escaped = label.replace("\\", "\\\\").replace('"', '\\"')
        return page.locator(f'flow-grid-tile-container[aria-label="{escaped}"]')

    def _connect(self, playwright: Playwright) -> Browser:
        if self.external_cdp_url:
            if not self._valid_cdp_url(self.external_cdp_url):
                raise GoogleFlowBrowserError(
                    "TH_MEDIA_FLOW_CDP_URL không hợp lệ; chỉ chấp nhận http(s)/ws(s)"
                )
            try:
                browser = playwright.chromium.connect_over_cdp(
                    self.external_cdp_url,
                    timeout=15_000,
                )
                if not browser.contexts:
                    raise GoogleFlowBrowserError("Trusted external Chrome không có browser context")
                if self.external_cdp_apply_imported_session:
                    self.sessions.apply_session(browser)
                return browser
            except GoogleFlowBrowserError:
                raise
            except Exception as exc:
                raise GoogleFlowBrowserError(
                    "Không kết nối được Trusted external Chrome qua CDP"
                ) from exc

        self.sessions.ensure_automation_browser(debug_port=self.debug_port)
        try:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{self.debug_port}",
                timeout=15_000,
            )
            self.sessions.apply_session(browser)
            return browser
        except Exception as exc:
            raise GoogleFlowBrowserError(
                f"Không kết nối được Chrome automation localhost:{self.debug_port}"
            ) from exc

    def _persist_runtime_session_best_effort(self, browser: Browser) -> None:
        try:
            self.sessions.persist_runtime_session(browser)
        except BrowserSessionError:
            # A completed provider asset must not be retried merely because the
            # auxiliary encrypted session refresh failed.
            return

    def _flow_page(self, browser: Browser) -> Page:
        pages = [page for context in browser.contexts for page in context.pages]
        for page in pages:
            if "flow.google.com" in page.url:
                if not self.external_cdp_url:
                    self.sessions.install_proxy_auth(page)
                return page
        if not browser.contexts:
            raise GoogleFlowBrowserError("Chrome automation không có browser context")
        page = browser.contexts[0].new_page()
        if not self.external_cdp_url:
            self.sessions.install_proxy_auth(page)
        return page

    @staticmethod
    def _project_id_from_url(url: str) -> str:
        match = FLOW_PROJECT_RE.search(url)
        return match.group(1) if match else ""

    @staticmethod
    def _auth_required(page: Page) -> bool:
        lower = page.url.lower()
        return "accounts.google.com" in lower or "signin" in lower

    def _enter_flow_app(self, page: Page) -> None:
        if self._auth_required(page):
            raise GoogleFlowBrowserError(
                "GOOGLE_FLOW_AUTH_REQUIRED: Session chưa đăng nhập Google Flow app"
            )
        if "flow.google.com/about" not in page.url.lower():
            return

        candidates = (
            "Create with Google Flow",
            "Try Google Flow",
            "Try in Google Flow",
        )
        entry = None
        for label in candidates:
            locator = page.get_by_role("button", name=label, exact=True)
            if locator.count():
                entry = locator.first
                break
        if entry is None:
            raise GoogleFlowBrowserError(
                "Không tìm thấy nút vào Google Flow app trên trang /about; UI có thể đã thay đổi"
            )
        entry.click()
        page.wait_for_timeout(2200)
        if self._auth_required(page):
            raise GoogleFlowBrowserError(
                "GOOGLE_FLOW_AUTH_REQUIRED: Cookie hiện tại chỉ mở được trang /about "
                "nhưng Google yêu cầu đăng nhập khi vào Flow app"
            )

    def _ensure_project(self, page: Page, project: Project) -> str:
        if project.provider_project_id:
            page.goto(
                FLOW_PROJECT_URL.format(project_id=project.provider_project_id),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            page.wait_for_timeout(900)
            if self._auth_required(page):
                raise GoogleFlowBrowserError(
                    "GOOGLE_FLOW_AUTH_REQUIRED: Session Google Flow đã hết hoặc thiếu cookie"
                )
            project_id = self._project_id_from_url(page.url)
            if project_id == project.provider_project_id:
                return project_id

        page.goto(FLOW_HOME_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(900)
        self._normalize(page)
        self._enter_flow_app(page)
        self._normalize(page)

        project_id = self._project_id_from_url(page.url)
        if project_id:
            project.provider_project_id = project_id
            return project_id

        new_project = page.get_by_role("button", name="New project", exact=True)
        if not new_project.count():
            new_project = page.get_by_role("button", name="Create project", exact=True)
        if not new_project.count():
            new_project = page.get_by_role("button", name="Start new project", exact=True)
        if not new_project.count():
            visible = []
            try:
                visible = [
                    (button.inner_text() or button.get_attribute("aria-label") or "").strip()
                    for button in page.locator("button:visible").all()[:24]
                ]
            except Exception:
                visible = []
            detail = ", ".join(item for item in visible if item)[:500]
            raise GoogleFlowBrowserError(
                "Không tìm thấy nút New/Create project trên Google Flow app; "
                "UI có thể đã thay đổi" + (f"; visible buttons: {detail}" if detail else "")
            )

        new_project.first.click()
        page.wait_for_url(re.compile(r".*/project/.*"), timeout=20_000)
        page.wait_for_timeout(700)
        project_id = self._project_id_from_url(page.url)
        if not project_id:
            raise GoogleFlowBrowserError("Google Flow không trả về project ID sau khi tạo project")
        project.provider_project_id = project_id
        return project_id

    def _staged_file(self, source: Path, token: str) -> Path:
        source = source.resolve()
        try:
            source.relative_to(self.data_root)
        except ValueError as exc:
            raise GoogleFlowBrowserError("Asset render nằm ngoài TH Media data root") from exc
        if not source.is_file():
            raise GoogleFlowBrowserError(f"Không tìm thấy asset render: {source.name}")
        stat = source.stat()
        fingerprint = hashlib.sha256(
            f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()
        ).hexdigest()[:12]
        safe_token = re.sub(r"[^A-Za-z0-9_.-]+", "-", token).strip("-")[:80] or "asset"
        target = self.staging_root / f"{safe_token}-{fingerprint}{source.suffix.lower()}"
        if not target.is_file() or target.stat().st_size != stat.st_size:
            shutil.copy2(source, target)
        return target

    @staticmethod
    def _media_id_from_tile(tile: Locator) -> str:
        try:
            media = tile.locator("[data-media-id]")
            if media.count():
                return media.first.get_attribute("data-media-id", timeout=750) or ""
            # Flow has moved data-media-id between the media element and the tile
            # container in past UI revisions. Accept either location without using
            # presentation text (aria-label) as asset identity.
            return tile.get_attribute("data-media-id", timeout=750) or ""
        except PlaywrightError:
            # The Flow grid is live-updated while generation completes. A tile can
            # disappear between locator enumeration and attribute read. Treat such
            # stale/transient DOM nodes as absent and let the caller continue scanning.
            return ""

    def _wait_asset_tile(
        self,
        page: Page,
        label: str,
        *,
        timeout_seconds: float = 90.0,
    ) -> tuple[Locator, str]:
        deadline = time.monotonic() + timeout_seconds
        tile = self._tile_exact_label(page, label)
        while time.monotonic() < deadline:
            if tile.count():
                media_id = self._media_id_from_tile(tile.last)
                if media_id:
                    return tile.last, media_id
            page.wait_for_timeout(350)
        raise GoogleFlowBrowserTimeout(f"Upload asset {label} không hoàn tất")

    def _upload_asset(self, page: Page, source: Path, token: str) -> tuple[str, str]:
        staged = self._staged_file(source, token)
        label = staged.name
        existing = self._tile_exact_label(page, label)
        if existing.count():
            media_id = self._media_id_from_tile(existing.last)
            if media_id:
                return label, media_id

        self._normalize(page)
        add_media = self._visible_button(page, aria="Add media menu")
        if not add_media.count():
            raise GoogleFlowBrowserError("Không tìm thấy Add media menu")
        add_media.click()
        page.wait_for_timeout(150)
        upload = page.get_by_role("menuitem", name="Upload", exact=True)
        if not upload.count():
            raise GoogleFlowBrowserError("Không tìm thấy menu Upload của Google Flow")
        try:
            with page.expect_file_chooser(timeout=5_000) as chooser_info:
                upload.click()
            chooser_info.value.set_files(str(staged))
        except Exception as exc:
            raise GoogleFlowBrowserError(f"Không mở được file chooser cho {label}") from exc
        _tile, media_id = self._wait_asset_tile(page, label)
        return label, media_id

    def _ensure_direct_mode(self, page: Page) -> Locator:
        deadline = time.monotonic() + 15.0
        last_state = ""
        while time.monotonic() < deadline:
            self._normalize(page)
            trigger = self._visible_button(page, aria="Settings trigger")
            if not trigger.count():
                trigger = self._visible_button(page, aria="Settings")
            if trigger.count() and trigger.first.is_visible():
                return trigger.first

            agent = page.locator("button.agent-mode-chip:visible")
            if agent.count() and agent.first.is_visible():
                last_state = (agent.first.inner_text() or "").strip()
                try:
                    agent.first.click(timeout=1_500)
                    page.wait_for_timeout(300)
                except PlaywrightError:
                    page.wait_for_timeout(300)
                trigger = self._visible_button(page, aria="Settings trigger")
                if not trigger.count():
                    trigger = self._visible_button(page, aria="Settings")
                if trigger.count() and trigger.first.is_visible():
                    return trigger.first

            page.wait_for_timeout(350)

        raise GoogleFlowBrowserError(
            "Không tìm thấy generation Settings của Google Flow sau 15s"
            + (f"; agent state: {last_state}" if last_state else "")
        )

    def _select_radio(self, page: Page, label: str) -> None:
        radio = self._visible_radio(page, label)
        if not radio.count():
            raise GoogleFlowBrowserError(f"Google Flow không hỗ trợ tùy chọn {label}")
        radio.first.click()
        page.wait_for_timeout(100)

    @staticmethod
    def _settings_section(page: Page, label: str) -> Locator:
        return page.locator("div.settings-section:visible", has_text=label)

    def _select_scoped_radio(self, page: Page, scope: Locator, label: str) -> None:
        radio = scope.locator('[role="radio"]:visible').filter(has_text=label)
        if not radio.count():
            raise GoogleFlowBrowserError(f"Google Flow Settings không hỗ trợ tùy chọn {label}")
        radio.first.click()
        page.wait_for_timeout(100)

    def _select_model_from_button(
        self,
        page: Page,
        selector: Locator,
        model: str,
    ) -> None:
        if not selector.count():
            raise GoogleFlowBrowserError("Không tìm thấy bộ chọn model Google Flow")
        selector.first.click()
        page.wait_for_timeout(160)
        menuitems = page.locator('[role="menuitem"]:visible')
        item = menuitems.filter(has_text=model)
        if not item.count():
            visible_names = [
                (menuitems.nth(index).inner_text() or "").strip()
                for index in range(min(menuitems.count(), 12))
            ]
            page.keyboard.press("Escape")
            raise GoogleFlowBrowserError(
                f"Model Google Flow không khả dụng: {model}; menu hiện có: {visible_names}"
            )
        item.first.click()
        page.wait_for_timeout(140)

    def _configure_agent_image_defaults(
        self,
        page: Page,
        *,
        model: str,
        aspect_ratio: str,
        outputs: int,
    ) -> None:
        confirm = self._settings_section(page, "Confirm before generating")
        if confirm.count():
            never = confirm.locator("mat-radio-button:visible").filter(has_text="Never")
            if never.count():
                classes = never.first.get_attribute("class") or ""
                if "mat-mdc-radio-checked" not in classes:
                    never.first.click()
                    page.wait_for_timeout(100)

        section = self._settings_section(page, "Image generation default")
        if not section.count():
            raise GoogleFlowBrowserError(
                "Không tìm thấy Image generation default trong Google Flow Settings"
            )
        self._select_scoped_radio(page, section.first, aspect_ratio)
        self._select_scoped_radio(page, section.first, f"x{outputs}")
        model_button = section.first.get_by_role(
            "button",
            name="Image generation default model",
            exact=True,
        )
        self._select_model_from_button(page, model_button, model)

        save = page.get_by_role("button", name="Save", exact=True)
        if not save.count():
            raise GoogleFlowBrowserError("Không tìm thấy nút Save trong Google Flow Settings")
        save.first.click()
        page.wait_for_timeout(300)

    def _select_model(self, page: Page, model: str) -> None:
        selector = page.locator('button[aria-label="Select model family"]:visible')
        if not selector.count():
            raise GoogleFlowBrowserError("Không tìm thấy bộ chọn model Google Flow")
        selector.first.click()
        page.wait_for_timeout(120)

        menuitems = page.locator('[role="menuitem"]:visible')
        item = page.get_by_role("menuitem", name=model, exact=True)
        if not item.count():
            item = menuitems.filter(has_text=model)
        if not item.count():
            visible_names = [
                (menuitems.nth(index).inner_text() or "").strip()
                for index in range(min(menuitems.count(), 12))
            ]
            page.keyboard.press("Escape")
            raise GoogleFlowBrowserError(
                f"Model Google Flow không khả dụng: {model}; menu hiện có: {visible_names}"
            )
        item.first.click()
        page.wait_for_timeout(120)

    def _configure_video(
        self,
        page: Page,
        settings: FlowVideoSettings,
        *,
        input_mode: str,
    ) -> None:
        if settings.aspect_ratio not in FLOW_VIDEO_ASPECTS:
            raise GoogleFlowBrowserError(
                f"Google Flow Video không hỗ trợ aspect ratio {settings.aspect_ratio}"
            )
        if settings.resolution not in FLOW_VIDEO_RESOLUTIONS:
            raise GoogleFlowBrowserError(
                f"Google Flow Video không hỗ trợ resolution {settings.resolution}"
            )
        if settings.duration not in FLOW_VIDEO_DURATIONS:
            raise GoogleFlowBrowserError(
                f"Google Flow Video chỉ hỗ trợ duration {FLOW_VIDEO_DURATIONS}, "
                f"scene hiện là {settings.duration}s"
            )
        if settings.model not in FLOW_VIDEO_MODELS:
            raise GoogleFlowBrowserError(f"Video model chưa được map: {settings.model}")
        if settings.outputs not in {1, 2, 3, 4}:
            raise GoogleFlowBrowserError("Google Flow chỉ hỗ trợ x1 đến x4 output")

        trigger = self._ensure_direct_mode(page)
        if input_mode not in {"Frames", "Ingredients"}:
            raise GoogleFlowBrowserError(f"Video input mode không hợp lệ: {input_mode}")
        trigger.click()
        page.wait_for_timeout(160)
        self._select_radio(page, "Video")
        self._select_radio(page, input_mode)
        self._select_radio(page, settings.aspect_ratio)
        self._select_radio(page, settings.resolution)
        self._select_radio(page, f"{settings.duration}s")
        self._select_radio(page, f"x{settings.outputs}")
        self._select_model(page, settings.model)
        page.keyboard.press("Escape")
        page.wait_for_timeout(180)

    def _configure_image(
        self,
        page: Page,
        *,
        model: str,
        aspect_ratio: str,
        outputs: int = 1,
    ) -> None:
        if aspect_ratio not in FLOW_IMAGE_ASPECTS:
            raise GoogleFlowBrowserError(
                f"Google Flow Image không hỗ trợ aspect ratio {aspect_ratio}"
            )
        if model not in FLOW_IMAGE_MODELS:
            raise GoogleFlowBrowserError(f"Image model chưa được map: {model}")
        trigger = self._ensure_direct_mode(page)
        trigger.click()
        page.wait_for_timeout(400)

        agent_image_section = self._settings_section(page, "Image generation default")
        if agent_image_section.count():
            self._configure_agent_image_defaults(
                page,
                model=model,
                aspect_ratio=aspect_ratio,
                outputs=outputs,
            )
            return

        self._select_radio(page, "Image")
        self._select_radio(page, aspect_ratio)
        self._select_radio(page, f"x{outputs}")
        self._select_model(page, model)
        self._normalize(page)
        page.wait_for_timeout(180)

    def _bind_frame(self, page: Page, slot: str, label: str) -> None:
        self._normalize(page)
        slot_button = page.locator("button.empty-chip:visible").filter(has_text=slot)
        if not slot_button.count():
            raise GoogleFlowBrowserError(f"Không tìm thấy slot {slot} frame")
        slot_button.first.click()
        page.wait_for_timeout(250)
        option = page.locator('[role="option"]:visible').filter(has_text=label)
        if not option.count():
            page.keyboard.press("Escape")
            raise GoogleFlowBrowserError(f"Asset {label} không xuất hiện trong {slot} picker")
        option.first.click()
        page.wait_for_timeout(280)

    def _add_ingredient(self, page: Page, label: str) -> None:
        self._normalize(page)
        add = self._visible_button(page, aria="Add ingredients to the prompt box")
        if not add.count():
            raise GoogleFlowBrowserError("Không tìm thấy Add ingredients")
        add.first.click()
        page.wait_for_timeout(220)
        option = page.locator('[role="option"]:visible').filter(has_text=label)
        if not option.count():
            page.keyboard.press("Escape")
            raise GoogleFlowBrowserError(f"Ingredient {label} không xuất hiện trong asset picker")
        option.first.click()
        page.wait_for_timeout(100)
        confirm = page.get_by_role("button", name="Add to prompt", exact=True)
        if confirm.count() and confirm.first.is_visible():
            confirm.first.click()
            page.wait_for_timeout(220)

    def _fill_prompt(self, page: Page, prompt: str) -> None:
        self._normalize(page)
        deadline = time.monotonic() + 10.0
        editor = page.locator(".ProseMirror:visible")
        while time.monotonic() < deadline and not editor.count():
            page.wait_for_timeout(250)
        if not editor.count():
            raise GoogleFlowBrowserError("Không tìm thấy prompt editor của Google Flow")

        target = editor.first
        try:
            target.focus(timeout=2_000)
            target.fill(prompt, timeout=5_000)
        except PlaywrightError:
            self._normalize(page)
            target = page.locator(".ProseMirror:visible").first
            try:
                target.focus(timeout=2_000)
                target.fill(prompt, timeout=5_000)
            except PlaywrightError as exc:
                raise GoogleFlowBrowserError(
                    "Không thể nhập prompt Google Flow vì editor đang bị overlay chặn"
                ) from exc
        page.wait_for_timeout(120)

    @staticmethod
    def _video_tiles(page: Page) -> Locator:
        # Keep the custom-element selector, but also tolerate Flow replacing the
        # internal component while retaining a real video media element.
        return page.locator("flow-grid-tile-container").filter(
            has=page.locator("flow-video-tile, video[data-media-id], video[src]")
        )

    @staticmethod
    def _image_tiles(page: Page) -> Locator:
        # Same resilience rule as video: media identity is more stable than an
        # Angular custom-element name.
        return page.locator("flow-grid-tile-container").filter(
            has=page.locator("flow-image-tile, img[data-media-id], img[src]")
        )

    @classmethod
    def _tile_media_ids(cls, tiles: Locator) -> set[str]:
        """Return stable Google Flow asset identities for the current grid.

        aria-label is presentation text and is not unique: corrective regenerations
        can legitimately produce multiple tiles with the same label. data-media-id
        is the provider asset identity and must be used to detect a new output.
        """
        media_ids: set[str] = set()
        for index in range(tiles.count()):
            media_id = cls._media_id_from_tile(tiles.nth(index))
            if media_id:
                media_ids.add(media_id)
        return media_ids

    @classmethod
    def _new_tile_by_media_id(
        cls,
        tiles: Locator,
        previous_media_ids: set[str],
    ) -> Locator | None:
        for index in range(tiles.count()):
            tile = tiles.nth(index)
            media_id = cls._media_id_from_tile(tile)
            if media_id and media_id not in previous_media_ids:
                return tile
        return None

    def _submit(self, page: Page) -> None:
        start = self._visible_button(page, aria="Start generation")
        if not start.count() or start.first.is_disabled():
            raise GoogleFlowBrowserError("Start generation chưa sẵn sàng")
        self._wait_for_generation_slot()
        start = self._visible_button(page, aria="Start generation")
        if not start.count() or start.first.is_disabled():
            raise GoogleFlowBrowserError("Start generation không còn sẵn sàng sau pacing wait")
        start.first.click()
        page.wait_for_timeout(500)
        dialogs = page.locator('[role="dialog"]:visible')
        if not dialogs.count():
            return
        dialog = dialogs.last
        for label in ("Generate", "Confirm", "Continue", "Start generation"):
            button = dialog.get_by_role("button", name=label, exact=False)
            if button.count() and button.first.is_visible() and not button.first.is_disabled():
                button.first.click()
                page.wait_for_timeout(350)
                return

    def _new_tile_timeout_seconds(self) -> float:
        # A Flow job can sit queued/pending without exposing data-media-id yet.
        # The old 120s hard cap could therefore report a false failure while
        # Google Flow was still legitimately generating the asset. The configured
        # provider timeout is the single source of truth for the whole generation.
        return max(30.0, self.generation_timeout_seconds)

    @staticmethod
    def _generation_pending(page: Page) -> bool:
        pending = page.locator(
            "flow-pending-tile:visible, .loading-percentage:visible, "
            '.progress-bar:visible, button[aria-label="Stop"]:visible'
        )
        return bool(pending.count())

    @staticmethod
    def _error_fingerprints(page: Page) -> tuple[str, ...]:
        errors = page.locator(
            '.error-tile-content:visible, [data-status="failed"]:visible, '
            '[data-state="failed"]:visible, [class*="generation-error"]:visible'
        )
        values: list[str] = []
        for index in range(errors.count()):
            item = errors.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = " ".join((item.inner_text() or "").split())
            except PlaywrightError:
                continue
            if text:
                values.append(text[:700])

        agent_failures = page.get_by_text(
            "The agent failed. Please try again.",
            exact=True,
        )
        for index in range(agent_failures.count()):
            item = agent_failures.nth(index)
            try:
                if not item.is_visible():
                    continue
                text = " ".join((item.inner_text() or "").split())
            except PlaywrightError:
                continue
            if text:
                values.append(text[:700])
        return tuple(values)

    @staticmethod
    def _new_error_text(
        current: tuple[str, ...],
        baseline: tuple[str, ...],
    ) -> str:
        before = Counter(baseline)
        for text in current:
            if before[text] > 0:
                before[text] -= 1
                continue
            return text
        return ""

    def _wait_new_tile(
        self,
        page: Page,
        *,
        kind: str,
        previous_media_ids: set[str],
        previous_error_fingerprints: tuple[str, ...] = (),
    ) -> Locator:
        tiles = self._video_tiles(page) if kind == "video" else self._image_tiles(page)
        deadline = time.monotonic() + self._new_tile_timeout_seconds()
        pending_observed = False
        while time.monotonic() < deadline:
            tile = self._new_tile_by_media_id(tiles, previous_media_ids)
            if tile is not None:
                return tile
            error_text = self._new_error_text(
                self._error_fingerprints(page),
                previous_error_fingerprints,
            )
            if error_text:
                failure = classify_flow_generation_failure(error_text)
                self._trip_generation_circuit(failure)
                raise GoogleFlowBrowserError(f"[{failure.code}] {failure.detail}")
            pending_observed = pending_observed or self._generation_pending(page)
            page.wait_for_timeout(300)
        current_media_ids = self._tile_media_ids(tiles)
        raise GoogleFlowBrowserTimeout(
            f"Google Flow không tạo {kind} tile mới có media id trong "
            f"{int(self._new_tile_timeout_seconds())}s; "
            f"trước={len(previous_media_ids)}, hiện tại={len(current_media_ids)}, "
            f"đã thấy pending={pending_observed}"
        )

    def _wait_completed_tile(self, page: Page, tile: Locator, *, kind: str) -> Locator:
        deadline = time.monotonic() + self.generation_timeout_seconds
        while time.monotonic() < deadline:
            if not tile.count():
                page.wait_for_timeout(600)
                continue
            progress = tile.locator(".progress-bar")
            error_indicators = tile.locator(
                '[role="alert"], .error-tile-content, .error-title, '
                '[data-status="failed"], [data-state="failed"], '
                '[class*="generation-error"], button[aria-label*="Retry" i], '
                'button:has-text("Try again"), button:has-text("Retry")'
            )
            visible_errors = [
                error_indicators.nth(index)
                for index in range(error_indicators.count())
                if error_indicators.nth(index).is_visible()
            ]
            if visible_errors:
                detail = " | ".join(
                    (
                        item.get_attribute("aria-label")
                        or item.inner_text()
                        or "Google Flow generation error"
                    ).strip()
                    for item in visible_errors[:3]
                )
                failure = classify_flow_generation_failure(detail)
                self._trip_generation_circuit(failure)
                raise GoogleFlowBrowserError(
                    f"[{failure.code}] Google Flow {kind} generation thất bại: {failure.detail}"
                )
            if not progress.count():
                pending_overlay = tile.locator(
                    "flow-pending-tile:visible, .loading-percentage:visible"
                )
                if not pending_overlay.count():
                    return tile
            page.wait_for_timeout(1_000)
        label = tile.get_attribute("aria-label") or kind
        raise GoogleFlowBrowserTimeout(
            f"Google Flow {kind} vẫn Queued/Processing sau "
            f"{int(self.generation_timeout_seconds)}s: {label}"
        )

    def _download_tile(
        self,
        page: Page,
        tile: Locator,
        *,
        target: Path,
        kind: str,
    ) -> None:
        self._normalize(page)
        tile.scroll_into_view_if_needed()
        tile.hover()
        page.wait_for_timeout(180)
        hotbar_timeout = min(
            60.0,
            max(15.0, self.generation_timeout_seconds),
        )
        click_deadline = time.monotonic() + hotbar_timeout
        last_error = ""
        while time.monotonic() < click_deadline:
            pending_overlay = tile.locator("flow-pending-tile:visible, .loading-percentage:visible")
            if pending_overlay.count():
                page.wait_for_timeout(350)
                continue
            more = tile.locator('button[aria-label^="More"]')
            if not more.count():
                page.wait_for_timeout(350)
                continue
            try:
                more.first.click(timeout=1_500)
                last_error = ""
                break
            except PlaywrightError as exc:
                last_error = str(exc)
                page.wait_for_timeout(350)
        else:
            raise GoogleFlowBrowserError(
                f"{kind} tile chưa sẵn sàng để mở More options sau 15s"
                + (f": {last_error[:300]}" if last_error else "")
            )
        page.wait_for_timeout(160)
        download = page.get_by_role("menuitem", name="Download", exact=True)
        if not download.count():
            raise GoogleFlowBrowserError(f"{kind} tile không có menu Download")

        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with page.expect_download(timeout=1_500) as download_info:
                download.first.click()
            transfer = download_info.value
        except Exception:
            page.wait_for_timeout(180)
            choice: Locator | None = None
            if kind == "image":
                for name in ("1K Original size", "Original size", "Original"):
                    candidate = page.get_by_role("menuitem", name=name, exact=False)
                    if candidate.count() and candidate.first.is_visible():
                        choice = candidate.first
                        break
            else:
                for name in ("Original", "720p", "Video"):
                    candidate = page.get_by_role("menuitem", name=name, exact=False)
                    if candidate.count() and candidate.first.is_visible():
                        choice = candidate.first
                        break
            if choice is None:
                raise GoogleFlowBrowserError(
                    f"Download {kind} mở submenu nhưng không có lựa chọn an toàn"
                ) from None
            with page.expect_download(timeout=30_000) as download_info:
                choice.click()
            transfer = download_info.value
        transfer.save_as(target)
        if not target.is_file() or target.stat().st_size <= 0:
            raise GoogleFlowBrowserError(f"File {kind} tải từ Google Flow bị rỗng")

    @staticmethod
    def _flow_duration(target_seconds: int) -> int:
        if target_seconds > max(FLOW_VIDEO_DURATIONS):
            raise GoogleFlowBrowserError(
                f"Scene {target_seconds}s vượt giới hạn 10s của một Google Flow clip; "
                "cần subclip planner trước khi render."
            )
        return next(duration for duration in FLOW_VIDEO_DURATIONS if duration >= target_seconds)

    @staticmethod
    def _trim_video(target: Path, duration_seconds: int) -> None:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise GoogleFlowBrowserError("Cần FFmpeg để trim Flow clip về đúng duration scene.")
        temporary = target.with_name(f".{target.stem}-trim{target.suffix}")
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(target),
            "-t",
            f"{duration_seconds:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(temporary),
        ]
        result = subprocess.run(  # nosec B603
            command,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            detail = result.stderr.decode("utf-8", errors="ignore")[-800:]
            raise GoogleFlowBrowserError(f"FFmpeg trim thất bại: {detail}")
        os.replace(temporary, target)

    @staticmethod
    def _extract_last_frame(source: Path, target: Path) -> None:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise GoogleFlowBrowserError("Cần FFmpeg để lấy last frame giữa các subclip")
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(  # nosec B603
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-sseof",
                "-0.08",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ],
            capture_output=True,
            timeout=90,
            check=False,
        )
        if result.returncode != 0 or not target.is_file():
            detail = result.stderr.decode("utf-8", errors="ignore")[-600:]
            raise GoogleFlowBrowserError(f"Không lấy được last frame subclip: {detail}")

    @staticmethod
    def _concat_subclips(parts: list[Path], target: Path) -> None:
        if len(parts) < 2:
            raise GoogleFlowBrowserError("Cần ít nhất hai subclip để ghép")
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise GoogleFlowBrowserError("Cần FFmpeg để ghép subclip")
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = target.with_name(f".{target.stem}-subclips.txt")
        temporary = target.with_name(f".{target.stem}-subclips{target.suffix}")
        manifest_lines: list[str] = []
        for part in parts:
            escaped = (
                part.resolve()
                .as_posix()
                .replace(
                    chr(39),
                    chr(39) + chr(92) + chr(39) + chr(39),
                )
            )
            manifest_lines.append(f"file '{escaped}'")
        manifest.write_text(
            "\n".join(manifest_lines) + "\n",
            encoding="utf-8",
        )
        try:
            result = subprocess.run(  # nosec B603
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(manifest),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "18",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(temporary),
                ],
                capture_output=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0 or not temporary.is_file():
                detail = result.stderr.decode("utf-8", errors="ignore")[-800:]
                raise GoogleFlowBrowserError(f"Ghép subclip thất bại: {detail}")
            os.replace(temporary, target)
        finally:
            manifest.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

    def _clear_generation_inputs(self, page: Page) -> None:
        self._normalize(page)
        clear = self._visible_button(page, aria="Clear prompt")
        if clear.count():
            try:
                clear.first.click(timeout=1_500)
                page.wait_for_timeout(180)
            except PlaywrightError:
                page.keyboard.press("Escape")
        self._normalize(page)

    def _generate_video_part(
        self,
        page: Page,
        project: Project,
        scene: Scene,
        *,
        prompt: str,
        semantic_duration: int,
        flow_duration: int,
        part_token: str,
        start_path: Path | None,
        end_path: Path | None,
        master_paths: list[Path],
    ) -> tuple[Path, str, str]:
        self._clear_generation_inputs(page)
        input_mode = "Frames" if start_path or end_path else "Ingredients"
        settings = FlowVideoSettings(
            model=project.settings.video_model or "Veo 3.1 - Lite [Lower Priority]",
            aspect_ratio=project.settings.aspect_ratio,
            resolution=project.settings.resolution,
            duration=flow_duration,
            outputs=1,
        )
        self._configure_video(page, settings, input_mode=input_mode)

        if start_path:
            start_label, _media_id = self._upload_asset(
                page,
                start_path,
                f"{project.id}-{part_token}-start",
            )
            self._bind_frame(page, "Start", start_label)
        if end_path:
            end_label, _media_id = self._upload_asset(
                page,
                end_path,
                f"{project.id}-{part_token}-end",
            )
            self._bind_frame(page, "End", end_label)
        if input_mode == "Ingredients":
            for index, reference_path in enumerate(master_paths):
                label, _media_id = self._upload_asset(
                    page,
                    reference_path,
                    f"{project.id}-{part_token}-master-{index + 1}",
                )
                self._add_ingredient(page, label)

        previous_media_ids = self._tile_media_ids(self._video_tiles(page))
        previous_errors = self._error_fingerprints(page)
        self._fill_prompt(page, prompt)
        self._submit(page)
        try:
            tile = self._wait_new_tile(
                page,
                kind="video",
                previous_media_ids=previous_media_ids,
                previous_error_fingerprints=previous_errors,
            )
            tile = self._wait_completed_tile(page, tile, kind="video")
            label = tile.get_attribute("aria-label") or part_token
            media_id = self._media_id_from_tile(tile) or uuid4().hex
            target = (
                self.data_root
                / "renders"
                / project.id
                / "subclips"
                / scene.id
                / f"{part_token}.mp4"
            )
            self._download_tile(page, tile, target=target, kind="video")
            if flow_duration > semantic_duration:
                self._trim_video(target, semantic_duration)
            return target, media_id, label
        finally:
            self._mark_generation_finished()

    def _generate_subclipped_video(
        self,
        page: Page,
        project: Project,
        scene: Scene,
        plans: list[FlowSubclip],
        *,
        start_source: str,
        target_source: str,
    ) -> FlowAsset:
        master_paths = [
            (self.data_root / relative).resolve()
            for relative in scene.image_plan.approved_reference_images
        ]
        current_start = (self.data_root / start_source).resolve() if start_source else None
        final_end = (self.data_root / target_source).resolve() if target_source else None
        parts: list[Path] = []
        last_media_id = ""
        last_label = ""

        for plan in plans:
            is_last = plan.index == plan.count
            part_token = f"{scene.id}-part-{plan.index:02d}"
            part_path, media_id, label = self._generate_video_part(
                page,
                project,
                scene,
                prompt=plan.prompt,
                semantic_duration=plan.semantic_duration,
                flow_duration=plan.flow_duration,
                part_token=part_token,
                start_path=current_start,
                end_path=final_end if is_last else None,
                master_paths=master_paths,
            )
            parts.append(part_path)
            last_media_id = media_id
            last_label = label
            if not is_last:
                frame_path = (
                    self.data_root
                    / "references"
                    / project.id
                    / "subclips"
                    / scene.id
                    / f"{part_token}-last.jpg"
                )
                self._extract_last_frame(part_path, frame_path)
                current_start = frame_path

        target = self.data_root / "renders" / project.id / f"{scene.id}.mp4"
        self._concat_subclips(parts, target)
        return FlowAsset(
            project_id=project.provider_project_id,
            media_id=last_media_id or uuid4().hex,
            label=f"{scene.id} · {len(parts)} subclips · {last_label}",
            kind="video",
            result_file=self._relative_file(target),
        )

    def _relative_file(self, path: Path) -> str:
        return path.resolve().relative_to(self.data_root).as_posix()

    def generate_video(self, project: Project, scene: Scene) -> FlowAsset:
        self._assert_generation_allowed()
        if scene.image_plan.status == "Blocked":
            raise GoogleFlowBrowserError(
                "Scene Image Plan đang Blocked; cần duyệt đủ Master References trước khi render"
            )
        start_source = (
            scene.image_plan.generated_start_frame
            or scene.image_plan.start_frame_source
            or scene.reference_image
        )
        target_source = scene.image_plan.generated_target_frame
        if (
            scene.image_plan.start_frame_strategy == "previous_accepted_end_frame"
            and not start_source
        ):
            raise GoogleFlowBrowserError(
                "Direct-continuation scene thiếu accepted Start frame từ scene trước"
            )

        with sync_playwright() as playwright:
            browser = self._connect(playwright)
            page = self._flow_page(browser)
            project_id = self._ensure_project(page, project)
            self._persist_runtime_session_best_effort(browser)

            if scene.duration > 10:
                plans = plan_flow_subclips(scene)
                asset = self._generate_subclipped_video(
                    page,
                    project,
                    scene,
                    plans,
                    start_source=start_source,
                    target_source=target_source,
                )
                self._persist_runtime_session_best_effort(browser)
                return asset

            model = project.settings.video_model or "Veo 3.1 - Lite [Lower Priority]"
            flow_duration = self._flow_duration(scene.duration)
            settings = FlowVideoSettings(
                model=model,
                aspect_ratio=project.settings.aspect_ratio,
                resolution=project.settings.resolution,
                duration=flow_duration,
                outputs=1,
            )
            input_mode = "Frames" if start_source or target_source else "Ingredients"
            self._configure_video(page, settings, input_mode=input_mode)

            if start_source:
                start_path = (self.data_root / start_source).resolve()
                start_label, _media_id = self._upload_asset(
                    page,
                    start_path,
                    f"{project.id}-{scene.id}-start",
                )
                self._bind_frame(page, "Start", start_label)

            if target_source:
                target_path = (self.data_root / target_source).resolve()
                end_label, _media_id = self._upload_asset(
                    page,
                    target_path,
                    f"{project.id}-{scene.id}-end",
                )
                self._bind_frame(page, "End", end_label)

            # In Ingredients mode the approved Project Masters are the visual
            # identity/world guards. In Frames mode the generated/accepted frame is the
            # physical pixel anchor and carries those already-approved identities.
            if input_mode == "Ingredients":
                for index, relative in enumerate(scene.image_plan.approved_reference_images):
                    reference_path = (self.data_root / relative).resolve()
                    label, _media_id = self._upload_asset(
                        page,
                        reference_path,
                        f"{project.id}-{scene.id}-master-{index + 1}",
                    )
                    self._add_ingredient(page, label)

            previous_media_ids = self._tile_media_ids(self._video_tiles(page))
            previous_errors = self._error_fingerprints(page)
            prompt = scene.render_prompt
            if flow_duration > scene.duration:
                prompt = (
                    f"{prompt}\n\nTIMING CONTRACT:\n"
                    f"Complete all source-authorized action by {scene.duration}.0 seconds. "
                    f"From {scene.duration}.0s to {flow_duration}.0s, hold the exact final "
                    "state without introducing a new action or event; TH Media will trim "
                    "that hold tail after download."
                )
            if scene.runtime_repair_instruction:
                prompt = f"{prompt}\n\nQC REPAIR INSTRUCTION:\n{scene.runtime_repair_instruction}"
            self._fill_prompt(page, prompt)
            self._submit(page)
            try:
                tile = self._wait_new_tile(
                    page,
                    kind="video",
                    previous_media_ids=previous_media_ids,
                    previous_error_fingerprints=previous_errors,
                )
                tile = self._wait_completed_tile(page, tile, kind="video")
                label = tile.get_attribute("aria-label") or f"{scene.id}-video"
                media_id = self._media_id_from_tile(tile) or uuid4().hex
                target = self.data_root / "renders" / project.id / f"{scene.id}.mp4"
                self._download_tile(page, tile, target=target, kind="video")
                if flow_duration > scene.duration:
                    self._trim_video(target, scene.duration)
                source_url = ""
                video = tile.locator("video")
                if video.count():
                    source_url = video.first.get_attribute("src") or ""
                self._persist_runtime_session_best_effort(browser)
                return FlowAsset(
                    project_id=project_id,
                    media_id=media_id,
                    label=label,
                    kind="video",
                    result_file=self._relative_file(target),
                    source_url=source_url,
                )
            finally:
                self._mark_generation_finished()

    def generate_image(
        self,
        project: Project,
        *,
        prompt: str,
        output_token: str,
        model: str = "Nano Banana 2",
        ingredient_files: list[Path] | None = None,
    ) -> FlowAsset:
        self._assert_generation_allowed()
        with sync_playwright() as playwright:
            browser = self._connect(playwright)
            page = self._flow_page(browser)
            project_id = self._ensure_project(page, project)
            self._persist_runtime_session_best_effort(browser)
            self._clear_generation_inputs(page)
            self._configure_image(
                page,
                model=model,
                aspect_ratio=project.settings.aspect_ratio,
                outputs=1,
            )
            for index, ingredient in enumerate(ingredient_files or []):
                label, _media_id = self._upload_asset(
                    page,
                    ingredient,
                    f"{project.id}-{output_token}-ingredient-{index + 1}",
                )
                self._add_ingredient(page, label)

            previous_media_ids = self._tile_media_ids(self._image_tiles(page))
            previous_errors = self._error_fingerprints(page)
            self._fill_prompt(page, prompt)
            self._submit(page)
            try:
                tile = self._wait_new_tile(
                    page,
                    kind="image",
                    previous_media_ids=previous_media_ids,
                    previous_error_fingerprints=previous_errors,
                )
                tile = self._wait_completed_tile(page, tile, kind="image")
                label = tile.get_attribute("aria-label") or output_token
                media_id = self._media_id_from_tile(tile) or uuid4().hex
                target = (
                    self.data_root / "references" / project.id / "generated" / f"{output_token}.jpg"
                )
                self._download_tile(page, tile, target=target, kind="image")
                source_url = ""
                image = tile.locator("img")
                if image.count():
                    source_url = image.first.get_attribute("src") or ""
                self._persist_runtime_session_best_effort(browser)
                return FlowAsset(
                    project_id=project_id,
                    media_id=media_id,
                    label=label,
                    kind="image",
                    result_file=self._relative_file(target),
                    source_url=source_url,
                )
            finally:
                self._mark_generation_finished()
