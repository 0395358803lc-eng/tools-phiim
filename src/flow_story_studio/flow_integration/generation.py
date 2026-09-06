"""Generation orchestration through the Flow CLI client."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ..flow_helpers import flow_prompt, reference_path
from ..flow_media import extract_last_frame, ffmpeg_path
from ..flow_ui_contract import FlowUIContractError, assert_safe_video_model
from ..models import Project, Scene
from ..providers.base import RenderResult
from .browser import (
    ExistingChromeAttachError,
    _ExistingChromeManager,
    can_attach_existing_chrome,
    ensure_live_flow_project,
)
from .errors import FlowIntegrationError, RenderCheckpoint
from .recovery import wait_for_browser_video

logger = logging.getLogger(__name__)


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
    manager: _ExistingChromeManager | None = None,
) -> Any:
    if manager is None and not can_attach_existing_chrome(self._chrome_port_file):
        raise RuntimeError("Existing Chrome remote debugging is unavailable")
    previous_media_type = self._active_media_type
    self._active_media_type = media_type

    async def submit(active_manager: _ExistingChromeManager) -> Any:
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
            manager=active_manager,
        )

    try:
        if manager is not None:
            return await submit(manager)
        async with _ExistingChromeManager(self._chrome_port_file) as owned:
            return await submit(owned)
    finally:
        self._active_media_type = previous_media_type


async def _generate_video(self, client: Any, **kwargs: Any) -> Any:
    browser_kwargs_ready = all(key in kwargs for key in ("aspect", "model", "timeout"))
    if (
        can_attach_existing_chrome(self._chrome_port_file)
        and hasattr(client, "_generate_via_browser")
        and browser_kwargs_ready
    ):
        try:
            return await _generate_with_existing_chrome(
                self, client, media_type="video", count=1, **kwargs
            )
        except ExistingChromeAttachError:
            # Chrome can expose a consent-enabled DevTools socket that Browser Use
            # can drive while Playwright CDP attach is rejected or times out.
            # This failure happens before any Flow UI action, so falling back
            # cannot duplicate a submitted generation.
            self._force_headed_browser = True
    configured_headless = os.getenv("FLOW_BROWSER_HEADLESS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    headless = configured_headless and not self._force_headed_browser
    try:
        return await client.generate_video(**kwargs, headless=headless)
    except Exception as exc:  # noqa: BLE001
        if not headless or "upstream HTTP 403" not in str(exc):
            raise
        self._force_headed_browser = True
        return await client.generate_video(**kwargs, headless=False)


def _prompt(scene: Scene) -> str:
    base = flow_prompt(scene)
    repair = scene.runtime_repair_instruction.strip()
    if not repair:
        return base
    return base + "\n\nRUNTIME QC REPAIR INSTRUCTION:\n" + repair


def _reference_path(self, value: str) -> str | None:
    return reference_path(self.data_root, value)


async def generate_reference_image(
    self, project: Project, reference_id: str, prompt: str
) -> str:
    """Generate and download one canonical reference image through Google Flow."""
    from .gflow_transport import (
        generate_reference_image_with_gflow,
        gflow_available,
        gflow_enabled,
    )

    if gflow_enabled():
        if not gflow_available():
            raise FlowIntegrationError(
                "gflow-cli không khả dụng; không fallback reference image sang Labs API cũ."
            )
        return await generate_reference_image_with_gflow(
            self,
            project,
            reference_id,
            prompt,
        )

    cookies, _ = self.vault.load()
    cdp_ready = can_attach_existing_chrome(self._chrome_port_file)
    if not cookies and not cdp_ready:
        raise FlowIntegrationError(
            "Chưa có phiên Flow hợp lệ; hãy mở Chrome đăng nhập Flow hoặc nhập cookie"
        )
    client = self._client(cookies or {})
    model = os.getenv("FLOW_REFERENCE_IMAGE_MODEL", "nano-banana-pro")
    try:
        if can_attach_existing_chrome(self._chrome_port_file):
            generated = await _generate_with_existing_chrome(
                self,
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

        target = self.data_root / "references" / project.id / "entities" / f"{reference_id}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            download_file, image.fife_url, target, cookies=cookies, kind="image"
        )
        if not target.is_file():
            raise FlowIntegrationError("Reference image không được lưu xuống máy")
        return target.resolve().relative_to(self.data_root).as_posix()
    except FlowIntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
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

    allow_paid = os.getenv("FLOW_ALLOW_PAID_VIDEO_MODELS", "").strip() == "1"
    try:
        safe_video_model = assert_safe_video_model(
            project.settings.video_model,
            allow_paid=allow_paid,
        )
    except FlowUIContractError as exc:
        raise FlowIntegrationError(str(exc)) from exc

    requested_transport = os.getenv("FLOW_VIDEO_TRANSPORT", "gflow").strip().lower()
    if requested_transport != "legacy":
        from .gflow_transport import generate_video_with_gflow, gflow_available

        if gflow_available():
            return await generate_video_with_gflow(
                self,
                project,
                scene,
                safe_model=safe_video_model,
                checkpoint=checkpoint,
            )
        if requested_transport == "gflow":
            raise FlowIntegrationError(
                "gflow-cli không khả dụng; không fallback sang Flow Labs API cũ."
            )

    cookies, _ = self.vault.load()
    cdp_ready = can_attach_existing_chrome(self._chrome_port_file)
    if not cookies and not cdp_ready:
        raise FlowIntegrationError(
            "Chưa có phiên Flow hợp lệ; hãy mở Chrome đăng nhập Flow hoặc nhập cookie"
        )
    upstream_project_id = project.flow_project_id
    if not upstream_project_id:
        if cdp_ready:
            upstream_project_id = await ensure_live_flow_project(
                self._chrome_port_file
            )
        else:
            legacy_client = self._client(cookies or {}, None)
            upstream_project_id = await legacy_client.create_project(
                project.name,
                media_type="video",
            )
        project.flow_project_id = upstream_project_id
    client = self._client(cookies or {}, upstream_project_id)
    scene.upstream_project_id = upstream_project_id
    if checkpoint:
        checkpoint(project, scene)
    duration = scene.duration if scene.duration in {4, 6, 8} else 8
    try:
        job = await _generate_video(
            self,
            client,
            prompt=_prompt(scene),
            aspect=project.settings.aspect_ratio,
            model=safe_video_model,
            duration=duration,
            image_path=_reference_path(self, scene.reference_image),
            timeout=self.timeout,
        )
        scene.provider_job_id = str(job.job_id)
        scene.upstream_workflow_id = str(getattr(job, "workflow_id", None) or "")
        scene.upstream_media_id = str(getattr(job, "media_id", None) or "")
        scene.upstream_resource_name = str(getattr(job, "resource_name", None) or "")
        if checkpoint:
            checkpoint(project, scene)
        completed = job
        output = self.data_root / "renders" / project.id / scene.id
        output.mkdir(parents=True, exist_ok=True)
        if cdp_ready:
            files = await wait_for_browser_video(
                self,
                upstream_project_id,
                job,
                output,
                timeout=self.timeout,
                poll_interval=5,
            )
            try:
                completed.status = "SUCCEEDED"
            except Exception as exc:  # noqa: BLE001
                logger.debug("Best-effort status update after browser poll success: %s", exc)
        else:
            if not getattr(job, "is_success", False):
                completed = await client.wait_for_video(
                    job,
                    timeout=self.timeout,
                    poll_interval=5,
                )
            files = await self._download_completed(
                client,
                completed,
                output,
                upstream_project_id,
            )
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
    except FlowIntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
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


def _ffmpeg_path() -> str | None:
    return ffmpeg_path()