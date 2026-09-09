"""FastAPI routes for project CRUD and storyboard editing."""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from .analysis_providers.xkiro import XKiroClient, XKiroError
from .film.image_plan import compile_project_image_plans
from .models import (
    AnalyzeRequest,
    ImageSettingsUpdate,
    Project,
    ReorderRequest,
    SceneLockUpdate,
    SceneUpdate,
    VideoProviderUpdate,
    VisionSettingsUpdate,
)
from .production_gate import (
    master_reference_qc_blockers,
    project_master_blockers,
)
from .providers.google_flow_browser import GoogleFlowBrowserProvider
from .render_queue import RenderQueue
from .service import StudioService
from .storage import ProjectStorage
from .visual_bible import canonical_reference_lock
from .visual_qc import VisualQCAnalyzer

LOGGER = logging.getLogger(__name__)


def build_project_router(
    *,
    storage: ProjectStorage,
    service: StudioService,
    xkiro: XKiroClient,
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
        if not queue.is_provider_configured(request.settings.provider):
            raise HTTPException(status_code=409, detail="Chưa cấu hình render provider.")
        try:
            project = await service.analyze_with_provider(request, xkiro)
        except XKiroError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        await queue.enqueue(project.id, [])
        return project

    @router.get("/api/projects/{project_id}/backups")
    async def list_project_backups(project_id: str) -> list[dict[str, object]]:
        required(project_id)
        return storage.backup_metadata(project_id)

    @router.post("/api/projects/{project_id}/backups/{backup_name}/restore", response_model=Project)
    async def restore_project_backup(project_id: str, backup_name: str) -> Project:
        required(project_id)
        try:
            return storage.restore_backup(project_id, backup_name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Backup not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/projects/{project_id}", response_model=Project)
    async def get_project(project_id: str) -> Project:
        return required(project_id)

    @router.patch("/api/projects/{project_id}/vision-settings", response_model=Project)
    async def update_vision_settings(
        project_id: str,
        patch: VisionSettingsUpdate,
    ) -> Project:
        if not xkiro.configured:
            raise HTTPException(
                status_code=409,
                detail="xKiro chưa được cấu hình.",
            )
        try:
            models = await xkiro.list_models(free_only=False)
        except XKiroError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception(
                "Unexpected xKiro Vision catalog failure project=%s",
                project_id,
            )
            raise HTTPException(
                status_code=502,
                detail="Không thể xác minh Vision model từ xKiro. Hãy thử lại.",
            ) from exc
        vision_models = {model.id for model in models if bool(model.capabilities.get("vision"))}
        if patch.vision_model not in vision_models:
            raise HTTPException(
                status_code=422,
                detail="Model đã chọn không còn khả dụng hoặc không hỗ trợ Vision.",
            )
        try:
            return service.update_vision_settings(project_id, patch.vision_model)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            LOGGER.exception(
                "Failed to persist Vision settings project=%s",
                project_id,
            )
            raise HTTPException(
                status_code=503,
                detail="Không thể lưu Vision model vào project. Hãy thử lại.",
            ) from exc

    @router.patch("/api/projects/{project_id}/image-settings", response_model=Project)
    async def update_image_settings(
        project_id: str,
        patch: ImageSettingsUpdate,
    ) -> Project:
        project = required(project_id)
        if not queue.is_provider_configured(project.settings.provider):
            raise HTTPException(
                status_code=409,
                detail="Render provider của project chưa sẵn sàng.",
            )
        provider = queue.providers.get(project.settings.provider)
        if provider is None:
            raise HTTPException(status_code=409, detail="Không tìm thấy render provider.")
        try:
            health = await provider.health()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Không đọc được image model từ provider: {exc}",
            ) from exc
        image_models = {
            str(model) for model in health.get("image_models", []) if str(model).strip()
        }
        if patch.image_model not in image_models:
            raise HTTPException(
                status_code=422,
                detail="Image model đã chọn không khả dụng trên provider hiện tại.",
            )
        try:
            return service.update_image_settings(project_id, patch.image_model)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.patch("/api/projects/{project_id}/video-settings", response_model=Project)
    async def update_video_settings(project_id: str, patch: VideoProviderUpdate) -> Project:
        try:
            return service.update_video_settings(
                project_id,
                patch.provider,
                patch.video_model,
                patch.resolution,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

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
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

    def _master_generation_ready(project: Project) -> None:
        if not xkiro.configured:
            raise HTTPException(
                status_code=409,
                detail="xKiro chưa được cấu hình; không thể Vision QC Master Reference.",
            )
        if not project.settings.vision_model:
            raise HTTPException(
                status_code=409,
                detail="Project chưa chọn Vision QC model cho Master Reference.",
            )
        if not project.settings.image_model:
            raise HTTPException(
                status_code=409,
                detail="Project chưa chọn Google Flow Image Model.",
            )
        if queue.references is None or queue.references.provider is None:
            raise HTTPException(
                status_code=409,
                detail="Chưa có image reference provider cho Master Reference.",
            )
        probe = getattr(queue.references.provider, "is_configured", None)
        if callable(probe) and not bool(probe()):
            raise HTTPException(
                status_code=409,
                detail="Google Flow Browser chưa sẵn sàng để tạo Master Reference.",
            )

    async def _generate_master_reference(
        project: Project,
        reference_id: str,
    ) -> Project:
        _master_generation_ready(project)
        reference = next(
            (item for item in project.visual_bible.references if item.id == reference_id),
            None,
        )
        if reference is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Master Reference")

        previous_status = reference.status
        previous_approved = reference.approved_reference
        try:
            await queue.references.ensure_reference(project, reference)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Tạo Master Reference thất bại: {exc}",
            ) from exc

        master_changed = (
            reference.status == "approved"
            and bool(reference.approved_reference)
            and (previous_status != "approved" or previous_approved != reference.approved_reference)
        )
        if master_changed:
            project = service.refresh_after_master_reference_change(
                project,
                reference.id,
            )
        else:
            compile_project_image_plans(project)
        return storage.save(project)

    @router.post(
        "/api/projects/{project_id}/visual-references/{reference_id}/generate",
        response_model=Project,
    )
    async def generate_master_reference(
        project_id: str,
        reference_id: str,
    ) -> Project:
        try:
            project = service.get_idle_project(project_id, "tạo Master Reference")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc
        return await _generate_master_reference(project, reference_id)

    @router.post(
        "/api/projects/{project_id}/visual-references/{reference_id}/image",
        response_model=Project,
    )
    async def upload_master_reference(
        project_id: str,
        reference_id: str,
        request: Request,
    ) -> Project:
        try:
            project = service.get_idle_project(project_id, "thay Master Reference")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

        reference = next(
            (item for item in project.visual_bible.references if item.id == reference_id),
            None,
        )
        if reference is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Master Reference")

        content_type = request.headers.get("content-type", "")
        extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        extension = extensions.get(content_type.split(";", 1)[0].lower())
        if not extension:
            raise HTTPException(
                status_code=415,
                detail="Chỉ hỗ trợ ảnh Master JPEG, PNG hoặc WebP",
            )
        content = await request.body()
        if not content or len(content) > 20 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail="Ảnh Master phải có dung lượng từ 1 byte đến 20 MB",
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
            raise HTTPException(
                status_code=415,
                detail="Image bytes do not match declared format",
            )

        digest = hashlib.sha256(content).hexdigest()[:16]
        target = (
            runtime_data_root
            / "references"
            / project.id
            / "masters"
            / reference.id
            / f"{digest}{extension}"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        relative = target.resolve().relative_to(runtime_data_root.resolve()).as_posix()
        if relative not in reference.reference_images:
            reference.reference_images.append(relative)

        if reference.entity_type == "location":
            reference.lock_text = canonical_reference_lock(project, reference)

        previous_approved = reference.approved_reference
        previous_status = reference.status
        previous_gate_safe = (
            previous_status == "approved"
            and bool(previous_approved)
            and not master_reference_qc_blockers(project, reference)
        )
        vision_available = bool(xkiro.configured and project.settings.vision_model)
        if vision_available:
            analyzer = VisualQCAnalyzer(runtime_data_root, xkiro)
            if previous_approved and previous_approved != relative:
                score, issues = await analyzer.inspect_reference_against_anchor_for_project(
                    project,
                    reference,
                    relative,
                    previous_approved,
                    model_id=project.settings.vision_model,
                )
            else:
                score, issues = await analyzer.inspect_reference_for_project(
                    project,
                    reference,
                    relative,
                    model_id=project.settings.vision_model,
                )
            reference.vision_score = score
            reference.vision_issues = issues
            reference.vision_model = project.settings.vision_model
            vision_unavailable = any(
                issue.code in {"VISION_UNAVAILABLE", "REFERENCE_MISSING"} for issue in issues
            )
            passed = not master_reference_qc_blockers(project, reference)
            if passed:
                reference.status = "approved"
                reference.approved_reference = relative
                reference.source_scene_id = ""
            elif previous_gate_safe:
                reference.status = "approved"
                reference.approved_reference = previous_approved
            else:
                reference.status = "candidate" if vision_unavailable else "rejected"
                reference.approved_reference = ""
        elif previous_gate_safe:
            reference.status = "approved"
            reference.approved_reference = previous_approved
        else:
            reference.status = "candidate"
            reference.approved_reference = ""

        master_changed = (
            reference.status == "approved"
            and bool(reference.approved_reference)
            and (previous_status != "approved" or previous_approved != reference.approved_reference)
        )
        if master_changed:
            project = service.refresh_after_master_reference_change(
                project,
                reference.id,
            )
        else:
            compile_project_image_plans(project)
        return storage.save(project)

    @router.get("/api/projects/{project_id}/master-gate")
    async def master_gate(project_id: str) -> dict[str, object]:
        project = required(project_id)
        blockers = project_master_blockers(project, data_root=runtime_data_root)
        return {
            "ready": not blockers,
            "blockers": blockers,
            "master_count": len(project.visual_bible.references),
            "approved_count": sum(
                reference.status == "approved" for reference in project.visual_bible.references
            ),
        }

    @router.get("/api/projects/{project_id}/scenes/{scene_id}/image/{kind}")
    async def scene_image(project_id: str, scene_id: str, kind: str) -> FileResponse:
        project = required(project_id)
        scene = next((item for item in project.scenes if item.id == scene_id), None)
        if not scene:
            raise HTTPException(status_code=404, detail="Không tìm thấy scene")
        if kind == "start":
            relative = (
                scene.image_plan.generated_start_frame
                or scene.image_plan.start_frame_source
                or scene.reference_image
            )
        elif kind == "target":
            relative = scene.image_plan.generated_target_frame
        else:
            raise HTTPException(status_code=404, detail="Image kind không hợp lệ")
        if not relative:
            raise HTTPException(status_code=404, detail="Scene chưa có ảnh")
        target = (runtime_data_root / relative).resolve()
        try:
            target.relative_to(runtime_data_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Đường dẫn ảnh không hợp lệ") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Không tìm thấy file ảnh")
        media_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        return FileResponse(
            target,
            media_type=media_types.get(target.suffix.lower(), "application/octet-stream"),
            filename=target.name,
        )

    @router.post(
        "/api/projects/{project_id}/scenes/{scene_id}/images/generate",
        response_model=Project,
    )
    async def generate_scene_images(project_id: str, scene_id: str) -> Project:
        try:
            project = service.get_idle_project(project_id, "tạo ảnh scene")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

        scene = next((item for item in project.scenes if item.id == scene_id), None)
        if not scene:
            raise HTTPException(status_code=404, detail="Không tìm thấy scene")
        provider = queue.providers.get(project.settings.provider)
        if bool(getattr(provider, "requires_master_gate", False)):
            master_blockers = project_master_blockers(project, data_root=runtime_data_root)
            if master_blockers:
                detail = " | ".join(master_blockers[:6])
                if len(master_blockers) > 6:
                    detail += f" | +{len(master_blockers) - 6} lỗi Master khác"
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "MASTER_GATE_BLOCKED: Chưa được tạo ảnh scene trước khi toàn bộ "
                        f"Project Masters đạt chuẩn. {detail}"
                    ),
                )
        if not queue.is_provider_configured(project.settings.provider):
            raise HTTPException(status_code=409, detail="Render provider của project chưa sẵn sàng")
        if not project.settings.image_model:
            raise HTTPException(
                status_code=409,
                detail="Hãy chọn và lưu Google Flow Image Model trước khi tạo ảnh scene.",
            )

        if not isinstance(provider, GoogleFlowBrowserProvider):
            raise HTTPException(
                status_code=409,
                detail="Provider hiện tại chưa hỗ trợ Scene Image generation",
            )
        try:
            result = await provider.generate_scene_images(project, scene)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Tạo ảnh scene thất bại: {exc}",
            ) from exc

        scene.image_plan.generated_start_frame = result.get("generated_start_frame", "")
        scene.image_plan.generated_target_frame = result.get("generated_target_frame", "")
        scene.image_plan.renderer_status = "configured"
        scene.image_plan.status = "Generated"
        upstream_project_id = result.get("upstream_project_id", "")
        if upstream_project_id:
            project.provider_project_id = upstream_project_id
            scene.upstream_project_id = upstream_project_id
        upstream_media_id = result.get("upstream_media_id", "")
        if upstream_media_id:
            scene.upstream_media_id = upstream_media_id
        return storage.save(project)

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
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

    @router.post("/api/projects/{project_id}/continuity", response_model=Project)
    async def continuity(project_id: str, auto_fix: bool | None = Query(default=None)) -> Project:
        try:
            return service.check_continuity(project_id, auto_fix)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=423, detail=str(exc)) from exc

    return router
