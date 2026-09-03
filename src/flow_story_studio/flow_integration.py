"""Embedded Google Flow CLI integration for the Windows desktop application."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .flow_credentials import CookieVault, FlowCredentialError, parse_cookie_input
from .flow_helpers import (
    flow_prompt,
    job_identifiers,
    reference_path,
    select_video_candidate,
)
from .flow_media import extract_last_frame, ffmpeg_path
from .models import FlowConnection, FlowVideoModel, Project, Scene
from .providers.base import RenderResult

VIDEO_MODELS = [
    FlowVideoModel(
        id="veo-3.1-lite-lower-priority",
        display_name="Veo 3.1 Lite · Lower priority",
        note="Tiết kiệm credit, hàng đợi có thể lâu hơn",
    ),
    FlowVideoModel(id="veo-3.1-fast", display_name="Veo 3.1 Fast", note="Nhanh"),
    FlowVideoModel(id="veo-3.1", display_name="Veo 3.1 Quality", note="Chất lượng cao"),
    FlowVideoModel(id="veo-3-fast", display_name="Veo 3 Fast", note="Nhanh"),
    FlowVideoModel(id="veo-3", display_name="Veo 3 Quality", note="Chất lượng cao"),
    FlowVideoModel(id="veo-3-lite", display_name="Veo 3 Lite", note="Tiết kiệm credit"),
    FlowVideoModel(id="veo-3.1-lite", display_name="Veo 3.1 Lite", note="Frames mode"),
    FlowVideoModel(id="veo-2-fast", display_name="Veo 2 Fast", note="Tương thích"),
    FlowVideoModel(id="veo-2", display_name="Veo 2", note="Tương thích"),
]


class FlowIntegrationError(RuntimeError):
    """A user-safe embedded Flow error."""


RenderCheckpoint = Callable[[Project, Scene], None]


def _parse_cookie_input(value: str) -> tuple[dict[str, str], list[dict[str, Any]] | None]:
    try:
        return parse_cookie_input(value)
    except FlowCredentialError as exc:
        raise FlowIntegrationError(str(exc)) from exc


def _add_development_flow_cli_path() -> None:
    """Find the sibling source tree in development; frozen builds bundle it."""
    sibling = Path(__file__).resolve().parents[3] / "tool-phiim" / "src"
    if sibling.is_dir() and str(sibling) not in sys.path:
        sys.path.insert(0, str(sibling))


def _flow_cli_available() -> bool:
    _add_development_flow_cli_path()
    return importlib.util.find_spec("flow_cli") is not None


class FlowCLIIntegration:
    def __init__(
        self,
        data_root: Path,
        timeout: int | None = None,
        credential_root: Path | None = None,
    ) -> None:
        self.data_root = data_root.resolve()
        credential_dir = (credential_root or self.data_root / "secrets").resolve()
        self.vault = CookieVault(credential_dir / "google-flow.cookies.bin")
        legacy_vault = CookieVault(self.data_root / "secrets" / "google-flow.cookies.bin")
        if legacy_vault.path != self.vault.path and not self.vault.path.exists():
            try:
                cookies, raw = legacy_vault.load()
                if cookies:
                    self._save_cookies(cookies, raw)
            except FlowCredentialError:
                # Keep the app usable so the user can replace a damaged legacy cookie.
                pass
        self.timeout = timeout or int(os.getenv("FLOW_RENDER_TIMEOUT", "900"))
        self._force_headed_browser = False

    @property
    def configured(self) -> bool:
        return self.vault.path.is_file()

    def _clear_cookies(self) -> None:
        try:
            self.vault.clear()
        except FlowCredentialError as exc:
            raise FlowIntegrationError(str(exc)) from exc

    def _save_cookies(self, cookies: dict[str, str], raw: list[dict[str, Any]] | None) -> None:
        try:
            self.vault.save(cookies, raw)
        except FlowCredentialError as exc:
            raise FlowIntegrationError(str(exc)) from exc

    def _load_cookies(self) -> tuple[dict[str, str], list[dict[str, Any]] | None]:
        try:
            return self.vault.load()
        except FlowCredentialError as exc:
            raise FlowIntegrationError(str(exc)) from exc

    @staticmethod
    def _browser_ready() -> bool:
        if not _flow_cli_available():
            return False
        bundled_root = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
        if bundled_root and any(Path(bundled_root).glob("chromium*/**/chrome.exe")):
            return True
        try:
            from flow_cli.cli.main import _find_playwright_chromium

            return bool(_find_playwright_chromium())
        except Exception:
            return False

    async def status(self, *, verify: bool = False) -> FlowConnection:
        available = _flow_cli_available()
        cookies, _ = self._load_cookies() if self.configured else ({}, None)
        connection = FlowConnection(
            configured=bool(cookies),
            authenticated=False,
            cookie_count=len(cookies),
            message="Chưa thêm cookie Google Flow",
            flow_cli_available=available,
            browser_ready=self._browser_ready(),
            models=VIDEO_MODELS,
        )
        if not available:
            connection.message = "Flow CLI chưa được đóng gói/cài đặt"
            return connection
        if not cookies:
            return connection
        if not verify:
            connection.message = "Đã lưu cookie an toàn; nhấn kiểm tra để xác thực"
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
            except Exception:
                pass
        return connection

    async def connect(self, cookie_input: str) -> FlowConnection:
        if not _flow_cli_available():
            raise FlowIntegrationError("Flow CLI chưa được cài hoặc đóng gói trong ứng dụng")
        cookies, raw = _parse_cookie_input(cookie_input)
        from flow_cli._auth import validate_cookies

        ok, message, _ = await asyncio.to_thread(validate_cookies, cookies, 15)
        if not ok:
            raise FlowIntegrationError(message)
        self._save_cookies(cookies, raw)
        return await self.status(verify=True)

    def disconnect(self) -> None:
        self._clear_cookies()

    def _client(self, cookies: dict[str, str], project_id: str | None = None) -> Any:
        self._apply_flow_ui_compatibility()
        from flow_cli._client import FlowClient

        client = FlowClient(cookies=cookies, project_id=project_id, timeout=30)
        _, raw = self.vault.load()
        if raw:
            client.raw_cookies = raw
        return client

    def _apply_flow_ui_compatibility(self) -> None:
        """Teach Flow CLI 0.6.0 how the current Radix tab controls expose selection."""
        import flow_cli._flow_ui as flow_ui

        current = tuple(flow_ui.SELECTED_OPTION_TEMPLATES)
        additions = (
            '[role="tab"][aria-selected="true"]:has-text("{label}")',
            '[role="tab"][data-state="active"]:has-text("{label}")',
        )
        flow_ui.SELECTED_OPTION_TEMPLATES = additions + tuple(
            item for item in current if item not in additions
        )
        if not getattr(flow_ui.FlowUI.verify_model_selection, "_studio_compat", False):
            original_verify_model = flow_ui.FlowUI.verify_model_selection

            def verify_model_selection(ui: Any, requested: str, selected: str) -> bool:
                # Material Symbols are rendered as text inside the current
                # model trigger and are not part of the selected model's name.
                cleaned = selected.replace("arrow_drop_down", "").strip()
                return original_verify_model(ui, requested, cleaned)

            verify_model_selection._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.verify_model_selection = verify_model_selection

        if not getattr(flow_ui.FlowUI.set_prompt, "_studio_compat", False):
            original_set_prompt = flow_ui.FlowUI.set_prompt

            async def set_prompt(ui: Any, prompt: str) -> None:
                editor = ui.page.locator(flow_ui.PROMPT_EDITOR_SELECTOR).first
                try:
                    if await editor.is_visible(timeout=2000):
                        await editor.fill(prompt, timeout=5000)
                        await ui.page.wait_for_timeout(300)
                        text = (await editor.inner_text(timeout=2000)).strip()
                        if prompt[:20] in text and prompt[-20:] in text:
                            return
                        await editor.click(timeout=1500)
                        await ui.page.keyboard.press("Control+A")
                        await ui.page.keyboard.insert_text(prompt)
                        await ui.page.wait_for_timeout(300)
                        text = (await editor.inner_text(timeout=2000)).strip()
                        if prompt[:20] in text and prompt[-20:] in text:
                            return
                        raise RuntimeError("Flow prompt editor did not retain the complete prompt")
                except RuntimeError:
                    raise
                except Exception:
                    pass
                await original_set_prompt(ui, prompt)

            set_prompt._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.set_prompt = set_prompt

        async def ensure_video_settings(ui: Any) -> None:
            video_tab = ui.page.locator('[role="tab"]:has-text("Video")').first
            image_tab = ui.page.locator('[role="tab"]:has-text("Image")').first
            tabs_open = False
            try:
                tabs_open = await video_tab.is_visible(timeout=300) and await image_tab.is_visible(
                    timeout=300
                )
            except Exception:
                pass
            if not tabs_open:
                trigger = ui.page.locator('button:has-text("Video ·")').first
                if not await trigger.is_visible(timeout=500):
                    menu_buttons = ui.page.locator('button:visible[aria-haspopup="menu"]')
                    trigger = None
                    for index in range((await menu_buttons.count()) - 1, -1, -1):
                        candidate = menu_buttons.nth(index)
                        try:
                            label = (await candidate.inner_text(timeout=500)).strip().lower()
                            if any(name in label for name in ("omni", "veo", "nano", "imagen")):
                                trigger = candidate
                                break
                        except Exception:
                            continue
                    if trigger is None:
                        raise RuntimeError("Could not find the Flow media settings button")
                await trigger.click(timeout=1500)
                await ui.page.wait_for_timeout(400)
            if not await video_tab.is_visible(timeout=1000):
                raise RuntimeError("Could not open the Flow video settings panel")
            if (await video_tab.get_attribute("data-state")) != "active":
                await video_tab.click(timeout=1500)
                await ui.page.wait_for_timeout(400)
            if (await video_tab.get_attribute("data-state")) != "active":
                raise RuntimeError("Could not select the Flow video tab")

        async def select_video_tab_option(ui: Any, label: str, kind: str) -> None:
            await ensure_video_settings(ui)
            option = ui.page.locator(f'[role="tab"]:has-text("{label}")').first
            if not await option.is_visible(timeout=1000):
                raise RuntimeError(f"Could not find Flow {kind} option {label!r}")
            if (await option.get_attribute("data-state")) != "active":
                await option.click(timeout=1500)
                await ui.page.wait_for_timeout(400)
            if (await option.get_attribute("data-state")) != "active":
                raise RuntimeError(f"Could not select Flow {kind} option {label!r}")

        if not getattr(flow_ui.FlowUI.select_aspect, "_studio_compat", False):

            async def select_aspect(ui: Any, aspect: str) -> None:
                await select_video_tab_option(ui, aspect, "aspect")

            select_aspect._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.select_aspect = select_aspect

        if not getattr(flow_ui.FlowUI.select_duration, "_studio_compat", False):

            async def select_duration(ui: Any, duration: int) -> None:
                await select_video_tab_option(ui, f"{duration}s", "duration")

            select_duration._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.select_duration = select_duration

        if not getattr(flow_ui.FlowUI.select_output_count, "_studio_compat", False):

            async def select_output_count(ui: Any, count: int) -> None:
                await select_video_tab_option(ui, f"x{count}", "output count")

            select_output_count._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.select_output_count = select_output_count

        if getattr(flow_ui.FlowUI.select_model, "_studio_compat", False):
            return

        async def select_video_model(ui: Any, model: str) -> None:
            await ensure_video_settings(ui)
            normalized = model.strip().lower().replace(" ", "-")
            candidates = list(flow_ui.VIDEO_MODEL_UI_LABELS.get(normalized, [model]))

            opened_nested_menu = False
            # The current Flow UI can default video generation to Omni, so the
            # nested model trigger is not guaranteed to contain the word "Veo".
            # It is the innermost visible menu button whose label names a model.
            buttons = ui.page.locator('button:visible[aria-haspopup="menu"]')
            for index in range((await buttons.count()) - 1, -1, -1):
                button = buttons.nth(index)
                try:
                    label = (await button.inner_text(timeout=500)).strip().lower()
                    if any(name in label for name in ("omni", "veo", "nano", "imagen")):
                        await button.click(timeout=1500)
                        await ui.page.wait_for_timeout(400)
                        opened_nested_menu = True
                        break
                except Exception:
                    continue

            templates = tuple(
                dict.fromkeys(flow_ui.IMAGE_MODEL_OPTION_TEMPLATES + flow_ui.MODEL_OPTION_TEMPLATES)
            )
            for candidate in candidates:
                for template in templates:
                    try:
                        option = ui.page.locator(template.format(label=candidate)).first
                        if await option.is_visible(timeout=1000):
                            await option.click(timeout=1500)
                            await ui.page.wait_for_timeout(400)
                            return
                    except Exception:
                        continue
            diagnostics = self.data_root / "diagnostics" / "flow-model-menu.png"
            diagnostics.parent.mkdir(parents=True, exist_ok=True)
            try:
                await ui.page.screenshot(path=str(diagnostics))
            except Exception:
                pass
            try:
                labels = [
                    text.strip()
                    for text in await ui.page.locator("button:visible").all_inner_texts()
                    if text.strip()
                ][:30]
            except Exception:
                labels = []
            await ui.page.keyboard.press("Escape")
            detail = " after opening nested model menu" if opened_nested_menu else ""
            raise RuntimeError(f"Could not find model {model!r}{detail}; visible controls={labels}")

        select_video_model._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_model = select_video_model

    @staticmethod
    def _prompt(scene: Scene) -> str:
        return flow_prompt(scene)

    def _reference_path(self, value: str) -> str | None:
        return reference_path(self.data_root, value)

    async def _generate_video(self, client: Any, **kwargs: Any) -> Any:
        configured_headless = os.getenv("FLOW_BROWSER_HEADLESS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        headless = configured_headless and not self._force_headed_browser
        try:
            return await client.generate_video(**kwargs, headless=headless)
        except Exception as exc:
            if not headless or "upstream HTTP 403" not in str(exc):
                raise
            # Google can reject Chromium headless as unusual activity while the
            # same authenticated session succeeds in regular Chromium. A 403
            # means no workflow was created, so this retry cannot duplicate a job.
            self._force_headed_browser = True
            return await client.generate_video(**kwargs, headless=False)

    @staticmethod
    def _job_identifiers(job: Any) -> set[str]:
        return job_identifiers(job)

    @staticmethod
    def _select_video_candidate(
        candidates: list[dict[str, str]], identifiers: set[str]
    ) -> dict[str, str] | None:
        return select_video_candidate(candidates, identifiers)

    async def _download_via_browser(
        self,
        upstream_project_id: str,
        job: Any,
        output: Path,
    ) -> list[Path]:
        """Recover the exact MP4 from Flow's authenticated project UI.

        Flow's internal workflow response sometimes contains a preview image
        instead of the final video media key. The project UI still exposes the
        correct authenticated redirect URL on its ``video`` element.
        """
        from flow_cli._browser import BrowserManager

        cookies, raw_cookies = self.vault.load()
        identifiers = self._job_identifiers(job)
        project_url = f"https://labs.google/fx/tools/flow/project/{upstream_project_id}"
        async with BrowserManager(
            headless=True,
            cookies=cookies,
            raw_cookies=raw_cookies,
        ) as browser:
            page = browser.page
            await page.goto(
                project_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            candidates: list[dict[str, str]] = []
            for attempt in range(3):
                await page.wait_for_timeout(5_000 if attempt == 0 else 3_000)
                raw_candidates = await page.locator("video[src]").evaluate_all(
                    """elements => elements.map(video => {
                      const anchor = video.closest('a[href*="/edit/"]');
                      const tile = video.closest('[data-tile-id]');
                      const src = video.getAttribute('src') || '';
                      const href = anchor?.getAttribute('href') || '';
                      let mediaKey = '';
                      try { mediaKey = new URL(src, location.href).searchParams.get('name') || ''; }
                      catch (_) {}
                      return {
                        src,
                        href,
                        tile_id: href.includes('/edit/')
                          ? href.split('/edit/')[1].split(/[?#/]/)[0]
                          : (tile?.getAttribute('data-tile-id') || ''),
                        media_key: mediaKey,
                      };
                    })"""
                )
                candidates = [
                    {
                        "src": str(item.get("src", "")),
                        "href": str(item.get("href", "")),
                        "tile_id": str(item.get("tile_id", "")),
                        "media_key": str(item.get("media_key", "")),
                    }
                    for item in raw_candidates
                    if isinstance(item, dict)
                ]
                selected = self._select_video_candidate(candidates, identifiers)
                if selected:
                    break
                if attempt < 2:
                    await page.reload(wait_until="domcontentloaded", timeout=30_000)
            else:
                selected = None
            if not selected:
                raise FlowIntegrationError(
                    "Flow đã tạo video nhưng không xác định được đúng video trong "
                    f"project (tìm thấy {len(candidates)} video)"
                )
            source_url = urljoin(project_url, selected["src"])
            response = await page.request.get(source_url, timeout=180_000)
            if response.status != 200:
                raise FlowIntegrationError(
                    f"Flow trả lỗi HTTP {response.status} khi tải MP4 qua phiên đăng nhập"
                )
            content_type = response.headers.get("content-type", "").lower()
            body = await response.body()
            if len(body) < 1_024 or b"ftyp" not in body[:32]:
                raise FlowIntegrationError(
                    "Dữ liệu tải từ Flow không phải MP4 hợp lệ "
                    f"({content_type or 'không rõ content-type'})"
                )
        output.mkdir(parents=True, exist_ok=True)
        media_key = selected.get("media_key") or selected.get("tile_id") or "recovered"
        safe_key = re.sub(r"[^0-9A-Za-z_-]", "", media_key)[:80] or "recovered"
        target = output / f"flow_{safe_key}.mp4"
        temporary = target.with_suffix(".mp4.part")

        def write_video() -> None:
            try:
                temporary.write_bytes(body)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

        await asyncio.to_thread(write_video)
        return [target]

    async def _download_completed(
        self,
        client: Any,
        completed: Any,
        output: Path,
        upstream_project_id: str,
    ) -> list[Path]:
        files: list[Path] = []
        try:
            files = [Path(item) for item in await client.download(completed.raw, dest_dir=output)]
        except Exception:
            files = []
        if any(item.suffix.lower() == ".mp4" and item.is_file() for item in files):
            return files
        return await self._download_via_browser(
            upstream_project_id,
            completed,
            output,
        )

    async def _recover_submitted(
        self,
        project: Project,
        scene: Scene,
    ) -> RenderResult | None:
        if not scene.upstream_project_id or not scene.provider_job_id:
            return None
        from types import SimpleNamespace

        saved_job = SimpleNamespace(
            job_id=scene.provider_job_id,
            workflow_id=scene.upstream_workflow_id or None,
            media_id=scene.upstream_media_id or None,
            resource_name=scene.upstream_resource_name or None,
            operation_name=None,
            raw={},
        )
        output = self.data_root / "renders" / project.id / scene.id
        try:
            files = await self._download_via_browser(
                scene.upstream_project_id,
                saved_job,
                output,
            )
        except Exception:
            return None
        video = next(
            (item for item in files if item.suffix.lower() == ".mp4" and item.is_file()),
            None,
        )
        if not video:
            return None
        result_file = video.resolve().relative_to(self.data_root).as_posix()
        last_frame_file = await self._extract_last_frame(project.id, scene.id, video)
        return RenderResult(
            job_id=scene.provider_job_id,
            result_url=f"/api/projects/{project.id}/scenes/{scene.id}/video",
            result_file=result_file,
            last_frame_file=last_frame_file,
            upstream_project_id=scene.upstream_project_id,
        )

    async def generate(
        self,
        project: Project,
        scene: Scene,
        checkpoint: RenderCheckpoint | None = None,
    ) -> RenderResult:
        cookies, _ = self.vault.load()
        if not cookies:
            raise FlowIntegrationError("Hãy thêm và xác thực cookie Google Flow trước khi render")
        recovered = await self._recover_submitted(project, scene)
        if recovered:
            return recovered
        client = self._client(cookies, project.flow_project_id or None)
        upstream_project_id = project.flow_project_id
        if not upstream_project_id:
            upstream_project_id = await client.create_project(project.name, media_type="video")
            project.flow_project_id = upstream_project_id
        scene.upstream_project_id = upstream_project_id
        if checkpoint:
            checkpoint(project, scene)
        duration = scene.duration if scene.duration in {4, 6, 8} else 8
        try:
            job = await self._generate_video(
                client,
                prompt=self._prompt(scene),
                aspect=project.settings.aspect_ratio,
                model=project.settings.video_model,
                duration=duration,
                image_path=self._reference_path(scene.reference_image),
                timeout=self.timeout,
            )
            scene.provider_job_id = str(job.job_id)
            scene.upstream_workflow_id = str(getattr(job, "workflow_id", None) or "")
            scene.upstream_media_id = str(getattr(job, "media_id", None) or "")
            scene.upstream_resource_name = str(getattr(job, "resource_name", None) or "")
            if checkpoint:
                checkpoint(project, scene)
            completed = job
            if not getattr(job, "is_success", False):
                completed = await client.wait_for_video(job, timeout=self.timeout, poll_interval=5)
            scene.upstream_workflow_id = str(
                getattr(completed, "workflow_id", None) or scene.upstream_workflow_id
            )
            scene.upstream_media_id = str(
                getattr(completed, "media_id", None) or scene.upstream_media_id
            )
            scene.upstream_resource_name = str(
                getattr(completed, "resource_name", None) or scene.upstream_resource_name
            )
            if checkpoint:
                checkpoint(project, scene)
            output = self.data_root / "renders" / project.id / scene.id
            output.mkdir(parents=True, exist_ok=True)
            files = await self._download_completed(
                client,
                completed,
                output,
                upstream_project_id,
            )
        except FlowIntegrationError:
            raise
        except Exception as exc:
            raise FlowIntegrationError(f"Google Flow: {type(exc).__name__}: {exc}") from exc
        video = next(
            (
                Path(item)
                for item in files
                if Path(item).suffix.lower() == ".mp4" and Path(item).is_file()
            ),
            None,
        )
        if not video:
            raise FlowIntegrationError("Google Flow hoàn tất nhưng không tải được tệp MP4")
        result_file = video.resolve().relative_to(self.data_root).as_posix()
        last_frame_file = await self._extract_last_frame(project.id, scene.id, video)
        return RenderResult(
            job_id=str(completed.job_id),
            result_url=f"/api/projects/{project.id}/scenes/{scene.id}/video",
            result_file=result_file,
            last_frame_file=last_frame_file,
            upstream_project_id=upstream_project_id,
        )

    async def _extract_last_frame(self, project_id: str, scene_id: str, video: Path) -> str:
        return await extract_last_frame(self.data_root, project_id, scene_id, video)

    @staticmethod
    def _ffmpeg_path() -> str | None:
        return ffmpeg_path()
