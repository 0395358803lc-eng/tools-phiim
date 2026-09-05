"""Download and recovery of completed Flow generations."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin

from ..flow_helpers import job_identifiers, select_video_candidate
from ..models import Project, Scene
from ..providers.base import RenderResult
from .errors import FlowIntegrationError


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
    identifiers = job_identifiers(job)
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
            selected = select_video_candidate(candidates, identifiers)
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
    except Exception:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
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