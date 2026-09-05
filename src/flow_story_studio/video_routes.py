"""FastAPI routes for scene rendering, video merge, and render queue control."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .flow_integration import FlowCLIIntegration
from .logging_config import get_logger
from .models import FinalVideo, GenerateRequest, Project, utc_now
from .render_queue import RenderQueue
from .storage import ProjectStorage
from .video_merger import VideoMergeError, VideoMerger

LOGGER = get_logger("video")


def build_video_router(
    *,
    storage: ProjectStorage,
    flow: FlowCLIIntegration,
    queue: RenderQueue,
    merger: VideoMerger,
    runtime_data_root: Path,
    merge_tasks: dict[str, asyncio.Task[None]],
    required: Callable[[str], Project],
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/projects/{project_id}/generate", response_model=Project, status_code=202)
    async def generate(project_id: str, request: GenerateRequest) -> Project:
        project = required(project_id)
        merge_task = merge_tasks.get(project_id)
        if merge_task and not merge_task.done():
            raise HTTPException(
                status_code=409,
                detail="Video tổng đang được ghép; hãy chờ hoàn tất trước khi render lại scene",
            )
        if project.settings.provider == "google-flow" and not flow.configured:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Hãy đăng nhập gflow + Chrome hoặc cấu hình cookie fallback "
                    "trước khi tạo video"
                ),
            )
        try:
            return await queue.enqueue(
                project_id,
                request.scene_ids,
                force_rerender=request.force_rerender,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/projects/{project_id}/scenes/{scene_id}/video")
    async def scene_video(project_id: str, scene_id: str) -> FileResponse:
        project = required(project_id)
        scene = next((item for item in project.scenes if item.id == scene_id), None)
        if not scene or not scene.result_file:
            raise HTTPException(status_code=404, detail="Scene chưa có video")
        target = (runtime_data_root / scene.result_file).resolve()
        try:
            target.relative_to(runtime_data_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Đường dẫn video không hợp lệ") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Không tìm thấy tệp video")
        return FileResponse(target, media_type="video/mp4", filename=target.name)

    @router.post("/api/projects/{project_id}/final-video", response_model=Project, status_code=202)
    async def merge_final_video(project_id: str) -> Project:
        project = required(project_id)
        active = merge_tasks.get(project_id)
        if active and not active.done():
            return project
        if not merger.ffmpeg_path():
            raise HTTPException(status_code=503, detail="Không tìm thấy FFmpeg để ghép video")
        try:
            merger.clips_for(project)
        except VideoMergeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        fingerprint = [
            (scene.id, scene.order, scene.result_file)
            for scene in sorted(project.scenes, key=lambda item: item.order)
        ]
        project.final_video = FinalVideo(
            status="Merging", progress=10, scene_count=len(project.scenes)
        )
        storage.save(project)

        async def run_merge() -> None:
            try:

                def update_progress(value: int) -> None:
                    latest_progress = storage.get(project_id)
                    if not latest_progress or latest_progress.final_video.status != "Merging":
                        return
                    bounded = max(10, min(95, value))
                    if bounded > latest_progress.final_video.progress:
                        latest_progress.final_video.progress = bounded
                        storage.save(latest_progress)

                result = await merger.merge(project, progress=update_progress)
                latest = storage.get(project_id)
                if not latest:
                    return
                current_fingerprint = [
                    (scene.id, scene.order, scene.result_file)
                    for scene in sorted(latest.scenes, key=lambda item: item.order)
                ]
                if current_fingerprint != fingerprint:
                    latest.final_video = FinalVideo(status="Ready", scene_count=len(latest.scenes))
                else:
                    latest.final_video = FinalVideo(
                        status="Completed",
                        progress=100,
                        result_url=f"/api/projects/{project_id}/final-video/file",
                        result_file=result.result_file,
                        scene_count=result.scene_count,
                        generated_at=utc_now(),
                    )
                storage.save(latest)
            except asyncio.CancelledError:
                latest = storage.get(project_id)
                if latest:
                    ready = all(
                        scene.status == "Accepted"
                        and scene.acceptance.status == "Accepted"
                        and bool(scene.result_file)
                        for scene in latest.scenes
                    )
                    latest.final_video = FinalVideo(
                        status="Ready" if ready else "NotReady",
                        scene_count=len(latest.scenes) if ready else 0,
                    )
                    storage.save(latest)
                raise
            except Exception as exc:
                LOGGER.exception("Final video merge failed project=%s", project_id)
                latest = storage.get(project_id)
                if latest:
                    message = str(exc) if isinstance(exc, VideoMergeError) else type(exc).__name__
                    latest.final_video = FinalVideo(
                        status="Failed",
                        error=f"Ghép video thất bại: {message}",
                        scene_count=len(latest.scenes),
                    )
                    storage.save(latest)

        merge_task = asyncio.create_task(run_merge())
        merge_tasks[project_id] = merge_task
        merge_task.add_done_callback(
            lambda _task, current_id=project_id: merge_tasks.pop(current_id, None)
        )
        return project

    @router.get("/api/projects/{project_id}/final-video/file")
    async def final_video_file(project_id: str) -> FileResponse:
        project = required(project_id)
        if project.final_video.status != "Completed" or not project.final_video.result_file:
            raise HTTPException(status_code=404, detail="Video tổng chưa hoàn tất")
        target = (runtime_data_root / project.final_video.result_file).resolve()
        try:
            target.relative_to(runtime_data_root.resolve())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Đường dẫn video tổng không hợp lệ"
            ) from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Không tìm thấy tệp video tổng")
        return FileResponse(
            target,
            media_type="video/mp4",
            filename=f"{project.id}-final.mp4",
        )

    @router.post("/api/projects/{project_id}/queue/pause", response_model=Project)
    async def pause(project_id: str) -> Project:
        required(project_id)
        queue.pause(project_id)
        return required(project_id)

    @router.post("/api/projects/{project_id}/queue/resume", response_model=Project)
    async def resume(project_id: str) -> Project:
        required(project_id)
        queue.resume(project_id)
        return required(project_id)

    return router
