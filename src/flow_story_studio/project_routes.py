"""FastAPI routes for project CRUD and storyboard editing."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response

from .analysis_providers.xkiro import XKiroClient, XKiroError
from .flow_integration import FlowCLIIntegration
from .models import (
    AnalyzeRequest,
    Project,
    ReorderRequest,
    SceneLockUpdate,
    SceneUpdate,
    VideoProviderUpdate,
)
from .render_queue import RenderQueue
from .service import StudioService
from .storage import ProjectStorage


def build_project_router(
    *,
    storage: ProjectStorage,
    service: StudioService,
    xkiro: XKiroClient,
    flow: FlowCLIIntegration,
    queue: RenderQueue,
    runtime_data_root: Path,
    required: Callable[[str], Project],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects")
    async def list_projects() -> list[dict[str, object]]:
        return storage.list()

    @router.post("/api/projects/analyze", response_model=Project, status_code=201)
    async def analyze(request: AnalyzeRequest) -> Project:
        try:
            return await service.analyze_with_provider(request, xkiro)
        except XKiroError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/api/projects/auto-pipeline", response_model=Project, status_code=201)
    async def auto_pipeline(request: AnalyzeRequest) -> Project:
        if request.settings.provider == "google-flow" and not flow.configured:
            raise HTTPException(
                status_code=409,
                detail="Hãy thêm và xác thực cookie Google Flow trước khi chạy Auto pipeline",
            )
        try:
            project = await service.analyze_with_provider(request, xkiro)
        except XKiroError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await queue.enqueue(project.id, [])
        return project

    @router.get("/api/projects/{project_id}", response_model=Project)
    async def get_project(project_id: str) -> Project:
        return required(project_id)

    @router.patch("/api/projects/{project_id}/video-settings", response_model=Project)
    async def update_video_settings(project_id: str, patch: VideoProviderUpdate) -> Project:
        project = required(project_id)
        project.settings.provider = patch.provider
        project.settings.video_model = patch.video_model
        return storage.save(project)

    @router.delete("/api/projects/{project_id}", status_code=204)
    async def delete_project(
        project_id: str, purge_artifacts: bool = Query(default=False)
    ) -> Response:
        if not storage.delete(project_id):
            raise HTTPException(status_code=404, detail="Project not found")
        if purge_artifacts:
            for relative in (Path("renders") / project_id, Path("references") / project_id):
                target = (runtime_data_root / relative).resolve()
                try:
                    target.relative_to(runtime_data_root.resolve())
                except ValueError:
                    continue
                shutil.rmtree(target, ignore_errors=True)
            final_video = (runtime_data_root / "final-videos" / f"{project_id}.mp4").resolve()
            try:
                final_video.relative_to(runtime_data_root.resolve())
                final_video.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        return Response(status_code=204)

    @router.patch("/api/projects/{project_id}/scenes/{scene_id}", response_model=Project)
    async def update_scene(project_id: str, scene_id: str, patch: SceneUpdate) -> Project:
        try:
            return service.update_scene(project_id, scene_id, patch)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Không tìm thấy project hoặc scene"
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

    @router.patch("/api/projects/{project_id}/scenes/{scene_id}/lock", response_model=Project)
    async def update_scene_lock(project_id: str, scene_id: str, patch: SceneLockUpdate) -> Project:
        try:
            return service.set_scene_lock(project_id, scene_id, patch.locked)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Không tìm thấy project hoặc scene"
            ) from exc

    @router.post("/api/projects/{project_id}/scenes/{scene_id}/reference", response_model=Project)
    async def upload_reference(project_id: str, scene_id: str, request: Request) -> Project:
        project = required(project_id)
        scene = next((item for item in project.scenes if item.id == scene_id), None)
        if not scene:
            raise HTTPException(status_code=404, detail="Không tìm thấy scene")
        content_type = request.headers.get("content-type", "")
        extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        extension = extensions.get(content_type.split(";", 1)[0].lower())
        if not extension:
            raise HTTPException(status_code=415, detail="Chỉ hỗ trợ ảnh JPEG, PNG hoặc WebP")
        content = await request.body()
        if not content or len(content) > 20 * 1024 * 1024:
            raise HTTPException(
                status_code=413, detail="Ảnh phải có dung lượng từ 1 byte đến 20 MB"
            )
        is_valid_image = (
            extension == ".jpg"
            and content.startswith(b"\xff\xd8\xff")
            or extension == ".png"
            and content.startswith(b"\x89PNG\r\n\x1a\n")
            or extension == ".webp"
            and len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
        )
        if not is_valid_image:
            raise HTTPException(status_code=415, detail="Image bytes do not match declared format")
        target = runtime_data_root / "references" / project.id / f"{scene.id}-manual{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        scene.reference_image = target.resolve().relative_to(runtime_data_root).as_posix()
        return storage.save(project)

    @router.post("/api/projects/{project_id}/reorder", response_model=Project)
    async def reorder(project_id: str, request: ReorderRequest) -> Project:
        try:
            return service.reorder(project_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/projects/{project_id}/continuity", response_model=Project)
    async def continuity(project_id: str, auto_fix: bool | None = Query(default=None)) -> Project:
        try:
            return service.check_continuity(project_id, auto_fix)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc

    return router
