"""Embedded Google Flow CLI integration for the Windows desktop application."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import shutil
import socket
import subprocess  # nosec B404 - required for local gflow argv execution
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

logger = logging.getLogger(__name__)

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
            except Exception as exc:
                logger.debug("Playwright disconnect cleanup failed: %s", exc)
        self._playwright = None
        self._browser = None
        self.context = None

    async def __aenter__(self) -> _ExistingChromeManager:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()


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
        self._active_media_type = "video"
        self.gflow_executable = shutil.which(os.getenv("GFLOW_EXECUTABLE", "gflow"))
        self.gflow_workdir = Path(
            os.getenv("GFLOW_WORKDIR", str(self.data_root.parent))
        ).resolve()
        self.gflow_profile = os.getenv("GFLOW_PROFILE", "default")
        self.gflow_debug_port = os.getenv("GFLOW_DEBUG_PORT", "9333")
        self._chrome_port_file = Path(
            os.getenv(
                "FLOW_CHROME_DEVTOOLS_ACTIVE_PORT",
                str(
                    Path.home()
                    / "AppData"
                    / "Local"
                    / "Google"
                    / "Chrome"
                    / "User Data"
                    / "DevToolsActivePort"
                ),
            )
        )

    @property
    def configured(self) -> bool:
        return self._gflow_profile_ready() or self.vault.path.is_file()

    def _gflow_profile_dir(self) -> Path:
        return self.gflow_workdir / ".gflow" / "profiles" / self.gflow_profile

    def _gflow_profile_ready(self) -> bool:
        return bool(self.gflow_executable) and self._gflow_profile_dir().is_dir()

    async def _run_gflow(
        self, args: list[str], *, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if not self.gflow_executable:
            raise FlowIntegrationError("gflow CLI is not installed or not on PATH")
        self.gflow_workdir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["GFLOW_DEBUG_PORT"] = self.gflow_debug_port
        command: list[str]
        creationflags = 0
        if os.name == "nt" and self.gflow_executable.lower().endswith((".cmd", ".bat")):
            launcher_dir = Path(self.gflow_executable).resolve().parent
            entrypoint = (
                launcher_dir
                / "node_modules"
                / "@swissmarley"
                / "gflow-cli"
                / "dist"
                / "src"
                / "index.js"
            )
            node = shutil.which("node")
            if not node or not entrypoint.is_file():
                raise FlowIntegrationError(
                    "gflow Windows launcher requires Node.js and the installed gflow entrypoint; "
                    "refusing cmd.exe fallback"
                )
            command = [node, str(entrypoint), *args]
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            command = [self.gflow_executable, *args]

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(  # nosec B603 - trusted executable, argv only
                command,
                cwd=self.gflow_workdir,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                creationflags=creationflags,
            )

        try:
            completed = await asyncio.to_thread(run)
        except subprocess.TimeoutExpired as exc:
            raise FlowIntegrationError(f"gflow timed out after {timeout}s") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gflow failed").strip()
            raise FlowIntegrationError(detail[:1200])
        return completed

    @staticmethod
    def _gflow_saved_paths(stdout: str) -> list[Path]:
        paths: list[Path] = []
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line.lower().startswith("saved "):
                continue
            candidate = Path(line[6:].strip().strip('\"'))
            if candidate.is_file():
                paths.append(candidate.resolve())
        return paths

    @staticmethod
    def _gflow_job_id(*parts: str) -> str:
        value = "-".join(parts).strip().lower()
        return re.sub(r"[^a-z0-9._-]+", "-", value).strip("-")[:96] or "gflow-job"

    async def _gflow_reference_image(self, project_id: str, reference_id: str, prompt: str) -> str:
        output = self.data_root / "references" / project_id / "entities"
        output.mkdir(parents=True, exist_ok=True)
        job_id = self._gflow_job_id(project_id, reference_id)
        model = os.getenv("FLOW_REFERENCE_IMAGE_MODEL", "nano-banana-pro")
        completed = await self._run_gflow(
            [
                "image", "--id", job_id, "--prompt", prompt,
                "--ratio", "1:1", "--model", model,
                "--outputs", "1", "--out", str(output),
                "--profile", self.gflow_profile, "--browser", "chrome",
                "--headed", "--timeout", str(min(self.timeout, 300)),
            ],
            timeout=min(self.timeout, 300) + 90,
        )
        images = [
            item for item in self._gflow_saved_paths(completed.stdout)
            if item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]
        if not images:
            raise FlowIntegrationError("gflow completed without a downloaded reference image")
        return images[0].relative_to(self.data_root).as_posix()

    async def _gflow_video(self, project: Project, scene: Scene) -> RenderResult:
        output = self.data_root / "renders" / project.id / scene.id
        output.mkdir(parents=True, exist_ok=True)
        job_id = self._gflow_job_id(project.id, scene.id)
        requested_duration = int(round(float(scene.duration or 8)))
        duration = min((4, 6, 8), key=lambda value: abs(value - requested_duration))
        args = [
            "video", "--id", job_id, "--prompt", self._prompt(scene),
            "--outputs", "1", "--out", str(output),
            "--profile", self.gflow_profile, "--browser", "chrome",
            "--headed", "--timeout", str(self.timeout),
            "--duration", str(duration),
        ]
        if project.settings.aspect_ratio:
            args.extend(["--ratio", project.settings.aspect_ratio])
        configured_model = (project.settings.video_model or "").strip()
        visible_model = os.getenv("GFLOW_VIDEO_MODEL", "").strip()
        if visible_model:
            args.extend(["--model", visible_model])
        elif configured_model and not configured_model.startswith("veo-"):
            args.extend(["--model", configured_model])
        reference = self._reference_path(scene.reference_image)
        if reference:
            args.extend(["--start-frame", reference])
        completed = await self._run_gflow(args, timeout=self.timeout + 120)
        videos = [
            item for item in self._gflow_saved_paths(completed.stdout)
            if item.suffix.lower() == ".mp4"
        ]
        if not videos:
            raise FlowIntegrationError("gflow completed without a downloaded MP4")
        video = videos[0]
        scene.provider_job_id = job_id
        last_frame_file = await self._extract_last_frame(project.id, scene.id, video)
        return RenderResult(
            job_id=job_id,
            result_url=f"/api/projects/{project.id}/scenes/{scene.id}/video",
            result_file=video.relative_to(self.data_root).as_posix(),
            last_frame_file=last_frame_file,
            upstream_project_id="",
        )

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
        if self._gflow_profile_ready():
            connection = FlowConnection(
                configured=True,
                authenticated=False,
                cookie_count=0,
                message="gflow Chrome profile is ready",
                flow_cli_available=True,
                browser_ready=True,
                models=VIDEO_MODELS,
            )
            if verify:
                try:
                    await self._run_gflow(
                        ["doctor", "--profile", self.gflow_profile, "--browser", "chrome"],
                        timeout=60,
                    )
                    connection.authenticated = True
                    connection.message = "gflow Chrome session is authenticated and ready"
                except FlowIntegrationError as exc:
                    connection.message = str(exc)
            return connection
        available = _flow_cli_available()
        legacy_configured = self.vault.path.is_file()
        cookies, _ = self._load_cookies() if legacy_configured else ({}, None)
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
            except Exception:  # nosec B110
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
                except Exception:  # nosec B110
                    pass
                await original_set_prompt(ui, prompt)

            set_prompt._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.set_prompt = set_prompt

        async def agent_settings_open(ui: Any) -> bool:
            try:
                settings_label = ui.page.get_by_text("Agent settings", exact=True).first
                if await settings_label.is_visible(timeout=300):
                    return True
            except Exception as exc:
                logger.debug("Flow Agent settings label probe failed: %s", exc)
            try:
                button = ui.page.locator('button[aria-label="Settings"]').first
                if not await button.is_visible(timeout=700):
                    return False
                await button.click(timeout=1500)
                await ui.page.wait_for_timeout(500)
                return await ui.page.get_by_text("Agent settings", exact=True).first.is_visible(
                    timeout=1000
                )
            except Exception:
                return False

        async def agent_section(ui: Any, label: str) -> Any:
            if not await agent_settings_open(ui):
                return None
            section_label = ui.page.get_by_text(label, exact=True).first
            if not await section_label.is_visible(timeout=800):
                return None
            return section_label.locator("xpath=parent::div[contains(@class,'settings-section')]")

        async def agent_select_toggle(ui: Any, label: str, value: str) -> bool:
            section = await agent_section(ui, label)
            if section is None:
                return False
            option = section.locator(f'button[role="radio"]:has-text("{value}")').first
            if not await option.is_visible(timeout=800):
                return False
            if (await option.get_attribute("aria-checked")) != "true":
                await option.click(timeout=1500)
                await ui.page.wait_for_timeout(300)
            if (await option.get_attribute("aria-checked")) != "true":
                raise RuntimeError(f"Flow Agent settings did not select {label}={value}")
            return True

        async def agent_select_model(ui: Any, media_type: str, model: str) -> bool:
            label = (
                "Image generation default"
                if media_type == "image"
                else "Video generation default"
            )
            section = await agent_section(ui, label)
            if section is None:
                return False
            aria = f"{label} model"
            picker = section.locator(f'button[aria-label="{aria}"]').first
            if not await picker.is_visible(timeout=800):
                return False
            normalized = model.strip().lower().replace(" ", "-")
            mapping = (
                flow_ui.IMAGE_MODEL_UI_LABELS
                if media_type == "image"
                else flow_ui.VIDEO_MODEL_UI_LABELS
            )
            candidates = list(mapping.get(normalized, [model]))
            current = (await picker.inner_text(timeout=500)).replace("arrow_drop_down", "").strip()
            if any(flow_ui.model_matches(model, candidate) for candidate in [current]):
                return True
            await picker.click(timeout=1500)
            await ui.page.wait_for_timeout(300)
            for candidate in candidates:
                option = ui.page.locator(f'[role="menuitem"]:has-text("{candidate}")').first
                try:
                    if await option.is_visible(timeout=700):
                        await option.click(timeout=1500)
                        await ui.page.wait_for_timeout(300)
                        updated = (
                            await picker.inner_text(timeout=500)
                        ).replace("arrow_drop_down", "").strip()
                        if not flow_ui.model_matches(model, updated):
                            raise RuntimeError(
                                f"Requested model {model!r}, but Flow Agent selected {updated!r}"
                            )
                        return True
                except RuntimeError:
                    raise
                except Exception as exc:
                    logger.debug("Flow Agent model candidate %r failed: %s", candidate, exc)
                    continue
            await ui.page.keyboard.press("Escape")
            raise RuntimeError(f"Could not select Flow Agent model {model!r}")

        async def agent_disable_confirmation_and_save(ui: Any) -> bool:
            section = await agent_section(ui, "Confirm before generating")
            if section is None:
                return False
            never = section.get_by_text("Never", exact=True).first
            try:
                radio = section.locator('input[type="radio"][value="2"]').first
                if await radio.is_visible(timeout=500):
                    if not await radio.is_checked():
                        await radio.click(timeout=1500)
                elif await never.is_visible(timeout=500):
                    await never.click(timeout=1500)
                else:
                    raise RuntimeError("Flow Agent confirmation setting 'Never' is unavailable")
            except Exception as exc:
                raise RuntimeError("Could not disable Flow Agent confirmation prompt") from exc
            save = ui.page.get_by_role("button", name="Save", exact=True).first
            if not await save.is_visible(timeout=800):
                raise RuntimeError("Flow Agent settings Save button is unavailable")
            await save.click(timeout=1500)
            await ui.page.wait_for_timeout(500)
            return True

        if not hasattr(flow_ui, "_studio_agent_original_select_aspect"):
            flow_ui._studio_agent_original_select_aspect = flow_ui.FlowUI.select_aspect
        if not hasattr(flow_ui, "_studio_agent_original_select_duration"):
            flow_ui._studio_agent_original_select_duration = flow_ui.FlowUI.select_duration
        if not hasattr(flow_ui, "_studio_agent_original_select_output_count"):
            flow_ui._studio_agent_original_select_output_count = flow_ui.FlowUI.select_output_count
        if not hasattr(flow_ui, "_studio_agent_original_select_model"):
            flow_ui._studio_agent_original_select_model = flow_ui.FlowUI.select_model
        if not hasattr(flow_ui, "_studio_agent_original_select_image_model"):
            flow_ui._studio_agent_original_select_image_model = flow_ui.FlowUI.select_image_model
        if not hasattr(flow_ui, "_studio_agent_original_get_selected_model"):
            flow_ui._studio_agent_original_get_selected_model = flow_ui.FlowUI.get_selected_model

        async def agent_aware_select_aspect(
            ui: Any, aspect: str, media_type: str | None = None
        ) -> None:
            active_media = media_type or self._active_media_type
            label = (
                "Image generation default"
                if active_media == "image"
                else "Video generation default"
            )
            if await agent_select_toggle(ui, label, aspect):
                return
            await flow_ui._studio_agent_original_select_aspect(
                ui, aspect, media_type=active_media
            )

        agent_aware_select_aspect._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_aspect = agent_aware_select_aspect

        async def agent_aware_select_duration(ui: Any, duration: int) -> None:
            if await ui.page.locator('button[aria-label="Settings"]').first.is_visible(timeout=300):
                # Current Agent UI has no duration control. The screenplay duration is
                # embedded in the production prompt; post-render QC verifies the result.
                return
            await flow_ui._studio_agent_original_select_duration(ui, duration)

        agent_aware_select_duration._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_duration = agent_aware_select_duration

        async def agent_aware_select_output_count(
            ui: Any, count: int, media_type: str | None = None
        ) -> None:
            active_media = media_type or self._active_media_type
            label = (
                "Image generation default"
                if active_media == "image"
                else "Video generation default"
            )
            if await agent_select_toggle(ui, label, f"x{count}"):
                await agent_disable_confirmation_and_save(ui)
                return
            await flow_ui._studio_agent_original_select_output_count(
                ui, count, media_type=active_media
            )

        agent_aware_select_output_count._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_output_count = agent_aware_select_output_count

        async def agent_aware_select_video_model(ui: Any, model: str) -> None:
            if await agent_select_model(ui, "video", model):
                return
            await flow_ui._studio_agent_original_select_model(ui, model)

        agent_aware_select_video_model._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_model = agent_aware_select_video_model

        async def agent_aware_select_image_model(ui: Any, model: str) -> None:
            if await agent_select_model(ui, "image", model):
                return
            await flow_ui._studio_agent_original_select_image_model(ui, model)

        agent_aware_select_image_model._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_image_model = agent_aware_select_image_model

        async def agent_aware_get_selected_model(ui: Any) -> str:
            label = (
                "Image generation default model"
                if self._active_media_type == "image"
                else "Video generation default model"
            )
            try:
                picker = ui.page.locator(f'button[aria-label="{label}"]').first
                if await picker.is_visible(timeout=500):
                    return (await picker.inner_text()).replace("arrow_drop_down", "").strip()
            except Exception as exc:
                logger.debug("Flow Agent selected-model probe failed: %s", exc)
            return await flow_ui._studio_agent_original_get_selected_model(ui)

        flow_ui.FlowUI.get_selected_model = agent_aware_get_selected_model

        if not getattr(flow_ui.FlowUI.click_generate, "_studio_agent_compat", False):
            original_click_generate = flow_ui.FlowUI.click_generate

            async def click_generate(ui: Any) -> None:
                start = ui.page.locator('button[aria-label="Start generation"]').first
                try:
                    if await start.is_visible(timeout=700) and not await start.is_disabled():
                        await start.click(timeout=5000)
                        return
                except Exception as exc:
                    logger.debug("Flow Agent start-generation button fallback: %s", exc)
                await original_click_generate(ui)

            click_generate._studio_agent_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.click_generate = click_generate

        async def ensure_video_settings(ui: Any) -> None:
            video_tab = ui.page.locator('[role="tab"]:has-text("Video")').first
            image_tab = ui.page.locator('[role="tab"]:has-text("Image")').first
            tabs_open = False
            try:
                tabs_open = await video_tab.is_visible(timeout=300) and await image_tab.is_visible(
                    timeout=300
                )
            except Exception:  # nosec B110
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
                        except Exception:  # nosec B112
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

        if not hasattr(flow_ui, "_studio_original_select_aspect"):
            flow_ui._studio_original_select_aspect = flow_ui.FlowUI.select_aspect

        if not getattr(flow_ui.FlowUI.select_aspect, "_studio_compat", False):

            async def select_aspect(ui: Any, aspect: str) -> None:
                if self._active_media_type == "image":
                    await ui._click_tool_toggle("image")
                    await flow_ui._studio_original_select_aspect(ui, aspect)
                    return
                await select_video_tab_option(ui, aspect, "aspect")

            select_aspect._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.select_aspect = select_aspect

        if not getattr(flow_ui.FlowUI.select_duration, "_studio_compat", False):

            async def select_duration(ui: Any, duration: int) -> None:
                await select_video_tab_option(ui, f"{duration}s", "duration")

            select_duration._studio_compat = True  # type: ignore[attr-defined]
            flow_ui.FlowUI.select_duration = select_duration

        if not hasattr(flow_ui, "_studio_original_select_output_count"):
            flow_ui._studio_original_select_output_count = flow_ui.FlowUI.select_output_count

        if not getattr(flow_ui.FlowUI.select_output_count, "_studio_compat", False):

            async def select_output_count(ui: Any, count: int) -> None:
                if self._active_media_type == "image":
                    await flow_ui._studio_original_select_output_count(ui, count)
                    return
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
                except Exception:  # nosec B112
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
                    except Exception:  # nosec B112
                        continue
            diagnostics = self.data_root / "diagnostics" / "flow-model-menu.png"
            diagnostics.parent.mkdir(parents=True, exist_ok=True)
            try:
                await ui.page.screenshot(path=str(diagnostics))
            except Exception:  # nosec B110
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

    def _can_attach_existing_chrome(self) -> bool:
        if not self._chrome_port_file.is_file():
            return False
        try:
            lines = self._chrome_port_file.read_text(encoding="utf-8").splitlines()
            port = int(lines[0].strip())
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except (OSError, ValueError, IndexError):
            return False

    async def _generate_with_existing_chrome(
        self,
        client: Any,
        *,
        media_type: str,
        prompt: str,
        aspect: str,
        model: str,
        duration: int = 8,
        image_path: str | None = None,
        timeout: int,
        count: int = 1,
    ) -> Any:
        if not self._can_attach_existing_chrome():
            raise RuntimeError("Existing Chrome remote debugging is unavailable")
        previous_media_type = self._active_media_type
        self._active_media_type = media_type
        try:
            async with _ExistingChromeManager(self._chrome_port_file) as manager:
                return await client._generate_via_browser(
                    prompt=prompt,
                    aspect=aspect,
                    model=model,
                    duration=duration,
                    image_path=image_path,
                    headless=False,
                    timeout=timeout,
                    media_type=media_type,
                    count=count,
                    manager=manager,
                )
        finally:
            self._active_media_type = previous_media_type

    async def _generate_video(self, client: Any, **kwargs: Any) -> Any:
        browser_kwargs_ready = all(key in kwargs for key in ("aspect", "model", "timeout"))
        if (
            self._can_attach_existing_chrome()
            and hasattr(client, "_generate_via_browser")
            and browser_kwargs_ready
        ):
            return await self._generate_with_existing_chrome(
                client, media_type="video", count=1, **kwargs
            )
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
        has_job = bool(scene.provider_job_id)
        has_project = bool(scene.upstream_project_id)
        if not has_job and not has_project:
            return None
        if not has_job or not has_project:
            raise FlowIntegrationError(
                "Existing Google Flow job identity is incomplete; refusing to submit a duplicate. "
                "Use force rerender only after confirming the previous job can be abandoned."
            )
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
        except Exception as exc:
            raise FlowIntegrationError(
                "Existing Google Flow job could not be recovered; no new generation was submitted. "
                "Retry recovery later or explicitly force rerender."
            ) from exc
        video = next(
            (item for item in files if item.suffix.lower() == ".mp4" and item.is_file()),
            None,
        )
        if not video:
            raise FlowIntegrationError(
                "Existing Google Flow job has no recoverable MP4 yet; "
                "no new generation was submitted. Retry recovery later "
                "or explicitly force rerender."
            )
        result_file = video.resolve().relative_to(self.data_root).as_posix()
        last_frame_file = await self._extract_last_frame(project.id, scene.id, video)
        return RenderResult(
            job_id=scene.provider_job_id,
            result_url=f"/api/projects/{project.id}/scenes/{scene.id}/video",
            result_file=result_file,
            last_frame_file=last_frame_file,
            upstream_project_id=scene.upstream_project_id,
        )

    async def generate_reference_image(
        self, project_id: str, reference_id: str, prompt: str
    ) -> str:
        """Generate and download one canonical reference image through Google Flow."""
        if self._gflow_profile_ready():
            return await self._gflow_reference_image(project_id, reference_id, prompt)
        cookies, _ = self.vault.load()
        if not cookies:
            raise FlowIntegrationError("Google Flow chưa được cấu hình để tạo reference image")
        client = self._client(cookies)
        model = os.getenv("FLOW_REFERENCE_IMAGE_MODEL", "nano-banana-pro")
        try:
            if self._can_attach_existing_chrome():
                generated = await self._generate_with_existing_chrome(
                    client,
                    media_type="image",
                    prompt=prompt,
                    aspect="1:1",
                    model=model,
                    timeout=min(self.timeout, 300),
                    count=1,
                )
                if isinstance(generated, list):
                    images = generated
                else:
                    images = await client.wait_for_images(
                        generated, count=1, timeout=min(self.timeout, 300)
                    )
            else:
                previous_media_type = self._active_media_type
                self._active_media_type = "image"
                try:
                    images = await client.generate_image(
                        prompt=prompt,
                        aspect="1:1",
                        count=1,
                        model=model,
                        headless=not self._force_headed_browser,
                        timeout=min(self.timeout, 300),
                    )
                finally:
                    self._active_media_type = previous_media_type
            image = images[0] if images else None
            if not image or not image.fife_url:
                raise FlowIntegrationError("Google Flow không trả về reference image tải được")
            from flow_cli._downloader import download_file

            target = self.data_root / "references" / project_id / "entities" / f"{reference_id}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                download_file, image.fife_url, target, cookies=cookies, kind="image"
            )
            if not target.is_file():
                raise FlowIntegrationError("Reference image không được lưu xuống máy")
            return target.resolve().relative_to(self.data_root).as_posix()
        except FlowIntegrationError:
            raise
        except Exception as exc:
            raise FlowIntegrationError(
                f"Google Flow reference image: {type(exc).__name__}: {exc}"
            ) from exc

    async def generate(
        self,
        project: Project,
        scene: Scene,
        checkpoint: RenderCheckpoint | None = None,
    ) -> RenderResult:
        if scene.provider_job_id or scene.upstream_project_id:
            recovered = await self._recover_submitted(project, scene)
            if recovered:
                return recovered
        if self._gflow_profile_ready():
            result = await self._gflow_video(project, scene)
            if checkpoint:
                checkpoint(project, scene)
            return result
        cookies, _ = self.vault.load()
        if not cookies:
            raise FlowIntegrationError("Hãy thêm và xác thực cookie Google Flow trước khi render")
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
