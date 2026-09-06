"""Download and recovery of completed Flow generations."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urljoin

from ..flow_helpers import job_identifiers, select_video_candidate
from ..models import Project, Scene
from ..providers.base import RenderResult
from .browser import _ExistingChromeManager, can_attach_existing_chrome
from .errors import FlowIntegrationError

logger = logging.getLogger(__name__)


async def _recover_page_content(
    page: Any,
    project_url: str,
    identifiers: set[str],
) -> tuple[dict[str, str], bytes]:
    await page.goto(
        project_url,
        wait_until="domcontentloaded",
        timeout=30_000,
    )
    candidates: list[dict[str, str]] = []
    selected: dict[str, str] | None = None
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
    return selected, body


async def _write_recovered_video(
    output: Path,
    selected: dict[str, str],
    body: bytes,
) -> list[Path]:
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


async def _download_via_browser(
    self,
    upstream_project_id: str,
    job: Any,
    output: Path,
    *,
    manager: _ExistingChromeManager | None = None,
) -> list[Path]:
    """Recover the exact MP4 from an authenticated Flow project page.

    Prefer the user's consent-enabled CDP Chrome session. Cookie-backed
    BrowserManager remains a fallback for legacy sessions.
    """
    identifiers = job_identifiers(job)
    project_url = f"https://flow.google.com/project/{upstream_project_id}"

    if manager is not None:
        if manager.context is None:
            raise FlowIntegrationError("Chrome CDP manager has no browser context")
        page = await manager.context.new_page()
        try:
            selected, body = await _recover_page_content(
                page,
                project_url,
                identifiers,
            )
        finally:
            await page.close()
        return await _write_recovered_video(output, selected, body)

    if can_attach_existing_chrome(self._chrome_port_file):
        async with _ExistingChromeManager(self._chrome_port_file) as browser:
            if browser.context is None:
                raise FlowIntegrationError("Chrome CDP manager has no browser context")
            page = await browser.context.new_page()
            try:
                selected, body = await _recover_page_content(
                    page,
                    project_url,
                    identifiers,
                )
            finally:
                await page.close()
        return await _write_recovered_video(output, selected, body)

    from flow_cli._browser import BrowserManager

    cookies, raw_cookies = self.vault.load()
    if not cookies:
        raise FlowIntegrationError(
            "Không có Chrome CDP session hoặc cookie Flow hợp lệ để recovery"
        )
    async with BrowserManager(
        headless=True,
        cookies=cookies,
        raw_cookies=raw_cookies,
    ) as browser:
        selected, body = await _recover_page_content(
            browser.page,
            project_url,
            identifiers,
        )
    return await _write_recovered_video(output, selected, body)


async def _download_completed(
    self,
    client: Any,
    completed: Any,
    output: Path,
    upstream_project_id: str,
) -> list[Path]:
    files: list[Path] = []
    try:
        files = [
            Path(item)
            for item in await client.download(completed.raw, dest_dir=output)
        ]
    except Exception:  # noqa: BLE001
        files = []
    if any(item.suffix.lower() == ".mp4" and item.is_file() for item in files):
        return files
    return await self._download_via_browser(
        upstream_project_id,
        completed,
        output,
    )


def _iter_migrated_generation_records(node: Any) -> Any:
    """Yield every CAE generation record nested inside a migrated Flow payload."""
    if not isinstance(node, list):
        return
    if (
        len(node) >= 6
        and node[3] == "CAE"
        and all(
            isinstance(node[index], str)
            and re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                node[index],
            )
            for index in (0, 1, 2)
        )
    ):
        yield node
    for child in node:
        yield from _iter_migrated_generation_records(child)


async def _download_migrated_record(
    client: Any,
    record: Any,
    out_dir: Path,
    *,
    max_redirects: int = 3,
) -> Path:
    """Download a migrated CAE clip while validating every redirect hop."""

    from gflow_cli.api.transports.ui_automation import _is_allowed_download_host

    page = getattr(client, "page", None)
    if page is None and hasattr(client, "request"):
        page = client
    media_id = getattr(record, "media_id", None)

    if hasattr(client, "_download_video") and page is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        result_path = await client._download_video(media_id, out_dir, page)
        if result_path and hasattr(result_path, "is_file") and result_path.is_file():
            return result_path

    if hasattr(client, "download_video"):
        out_dir.mkdir(parents=True, exist_ok=True)
        target_path = out_dir / f"{media_id}.mp4"
        result_path = await client.download_video(media_id, target_path)
        if result_path and hasattr(result_path, "is_file") and result_path.is_file():
            return result_path

    if page is None or not hasattr(page, "request"):
        raise FlowIntegrationError(
            "Migrated gflow recovery could not obtain an MP4: no client download method "
            "or page.request available."
        )

    failures: list[str] = []
    for initial_url in (getattr(record, "video_url", None), getattr(record, "poster_url", None)):
        if not initial_url:
            continue
        current = str(initial_url)
        for hop in range(max_redirects + 1):
            if not _is_allowed_download_host(current):
                failures.append("blocked non-Google media host")
                break

            response = await page.request.get(
                current,
                timeout=180_000,
                max_redirects=0,
            )
            if 300 <= response.status < 400:
                location = response.headers.get("location", "")
                if not location:
                    failures.append(f"HTTP {response.status} without Location")
                    break
                if hop >= max_redirects:
                    failures.append("too many signed media redirects")
                    break
                next_url = urljoin(current, location)
                if not _is_allowed_download_host(next_url):
                    failures.append("blocked redirect to non-Google media host")
                    break
                current = next_url
                continue

            if response.status >= 400:
                failures.append(f"HTTP {response.status}")
                break

            body = await response.body()
            if len(body) >= 8 and body[4:8] == b"ftyp":
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / f"{media_id}.mp4"
                path.write_bytes(body)
                return path

            failures.append(f"non-MP4 body ({len(body)} bytes)")
            break

    raise FlowIntegrationError(
        "Migrated gflow recovery could not obtain an MP4 from the signed "
        f"Google media URL ({'; '.join(failures) or 'no media URL'})."
    )


async def _recover_submitted_gflow(
    self,
    project: Project,
    scene: Scene,
) -> RenderResult:
    """Recover an already-submitted migrated Flow job from the project feed.

    Migrated project-load responses can carry many CAE generation records in one
    payload. Recovery enumerates every record and matches persisted media/workflow
    identity exactly before downloading anything.
    """
    from gflow_cli.api.transports.batchexecute import generation_record, parse_frames

    from .gflow_transport import (
        _flow_api_client,
        _validate_downloaded_mp4,
        resolve_gflow_profile_dir,
    )

    target_media_id = str(scene.upstream_media_id or scene.provider_job_id or "")
    target_workflow_id = str(scene.upstream_workflow_id or "")
    upstream_project_id = str(
        scene.upstream_project_id or project.flow_project_id or ""
    )
    if not target_media_id or not upstream_project_id:
        raise FlowIntegrationError(
            "Existing gflow job identity is incomplete; refusing duplicate generation."
        )

    output = self.data_root / "renders" / project.id / scene.id
    output.mkdir(parents=True, exist_ok=True)
    profile_path = resolve_gflow_profile_dir()
    latest: Any | None = None

    async with _flow_api_client(
        profile_dir=profile_path,
        headless=False,
        out_dir=output,
    ) as client:
        page = client.page

        async def inspect_response(response: Any) -> None:
            nonlocal latest
            url = str(getattr(response, "url", "") or "")
            if "batchexecute" not in url:
                return
            try:
                body = await response.text()
            except Exception:  # noqa: BLE001
                return
            for rpcid, payload in parse_frames(body):
                for raw_record in _iter_migrated_generation_records(payload):
                    try:
                        record = generation_record(rpcid, raw_record)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Skipping malformed generation record: %s", exc)
                        continue
                    media_matches = record.media_id == target_media_id
                    workflow_matches = (
                        bool(target_workflow_id)
                        and record.workflow_id == target_workflow_id
                    )
                    if media_matches or workflow_matches:
                        latest = record

        page.on("response", inspect_response)
        project_url = f"https://flow.google.com/project/{upstream_project_id}"
        try:
            for attempt in range(3):
                if attempt == 0:
                    await page.goto(
                        project_url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                else:
                    await page.reload(
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                await page.wait_for_timeout(4_000)
                if (
                    latest is not None
                    and bool(getattr(latest, "is_done", False))
                    and getattr(latest, "video_url", None)
                ):
                    break
        finally:
            page.remove_listener("response", inspect_response)

        if latest is None:
            raise FlowIntegrationError(
                "Existing gflow job was not found in the migrated Flow project feed; "
                "no new generation was submitted."
            )
        if not bool(getattr(latest, "is_done", False)):
            raise FlowIntegrationError(
                f"Existing gflow job is not complete yet (status={latest.status}); "
                "no new generation was submitted."
            )
        if not getattr(latest, "video_url", None):
            raise FlowIntegrationError(
                "Existing gflow job is complete but has no downloadable video URL yet; "
                "no new generation was submitted."
            )

        # Project-feed media URLs can add one or more Google-owned redirect
        # hops. Follow them manually while validating every Location target.
        video = (await _download_migrated_record(client, latest, output)).resolve()

    _validate_downloaded_mp4(video)
    scene.provider_job_id = str(latest.media_id)
    scene.upstream_media_id = str(latest.media_id)
    scene.upstream_workflow_id = str(latest.workflow_id)
    scene.upstream_project_id = str(latest.project_id)
    scene.upstream_resource_name = str(latest.media_id)
    project.flow_project_id = str(latest.project_id)

    try:
        result_file = video.relative_to(self.data_root).as_posix()
    except ValueError as exc:
        raise FlowIntegrationError(
            "Recovered gflow MP4 is outside the application data directory."
        ) from exc
    last_frame_file = await self._extract_last_frame(project.id, scene.id, video)
    return RenderResult(
        job_id=scene.provider_job_id,
        result_url=f"/api/projects/{project.id}/scenes/{scene.id}/video",
        result_file=result_file,
        last_frame_file=last_frame_file,
        upstream_project_id=scene.upstream_project_id,
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

    from .gflow_transport import gflow_available, gflow_enabled

    if gflow_enabled() and gflow_available():
        return await _recover_submitted_gflow(self, project, scene)

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
        (
            item
            for item in files
            if item.suffix.lower() == ".mp4" and item.is_file()
        ),
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


async def wait_for_browser_video(
    self,
    upstream_project_id: str,
    job: Any,
    output: Path,
    *,
    timeout: int,
    poll_interval: float = 5.0,
    manager: _ExistingChromeManager | None = None,
) -> list[Path]:
    """Wait for the exact submitted video using only the authenticated Flow UI."""
    deadline = asyncio.get_event_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            return await _download_via_browser(
                self,
                upstream_project_id,
                job,
                output,
                manager=manager,
            )
        except FlowIntegrationError as exc:
            last_error = exc
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))
    detail = f": {last_error}" if last_error else ""
    raise FlowIntegrationError(
        "Flow chưa xuất hiện MP4 khớp đúng job trên project page trước khi hết timeout"
        + detail
    )
