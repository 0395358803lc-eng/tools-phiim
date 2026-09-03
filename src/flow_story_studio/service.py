"""Application use cases connecting engines, storage, and prompt regeneration."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from .analysis_providers.xkiro import XKiroClient
from .engines.analyzer import analyze_story
from .engines.continuity import check_project
from .engines.prompt_generator import make_flow_prompt, make_visual_prompt
from .models import AnalyzeRequest, FinalVideo, Project, ReorderRequest, SceneUpdate
from .storage import ProjectStorage


class StudioService:
    def __init__(self, storage: ProjectStorage) -> None:
        self.storage = storage

    def analyze(self, request: AnalyzeRequest) -> Project:
        return self.storage.save(analyze_story(request))

    async def analyze_with_provider(
        self,
        request: AnalyzeRequest,
        xkiro: XKiroClient,
        progress: Callable[[str, str], None] | None = None,
    ) -> Project:
        if request.settings.analysis_provider == "xkiro":
            project = await xkiro.analyze(request, progress=progress)
            saved = self.storage.save(project)
            await xkiro.clear_checkpoint(request)
            return saved
        if progress:
            progress("Đang phân tích bằng engine offline", "info")
        return self.analyze(request)

    def get_required(self, project_id: str) -> Project:
        project = self.storage.get(project_id)
        if not project:
            raise KeyError(project_id)
        return project

    def update_scene(self, project_id: str, scene_id: str, patch: SceneUpdate) -> Project:
        project = self.get_required(project_id)
        scene_index = next(
            (index for index, scene in enumerate(project.scenes) if scene.id == scene_id), None
        )
        if scene_index is None:
            raise KeyError(scene_id)
        scene = project.scenes[scene_index]
        changes = patch.model_dump(exclude_none=True)
        protected = set(changes) - {"selected", "reference_image"}
        if scene.ai_locked and protected:
            raise PermissionError(
                "Scene đang được AI Continuity Lock bảo vệ; hãy mở khóa trước khi chỉnh sửa"
            )
        for key, value in changes.items():
            setattr(scene, key, value)
        location = next(
            (item for item in project.locations if item.id == scene.location_id),
            project.locations[0],
        )
        characters = [item for item in project.characters if item.id in scene.characters]
        prompt_fields = {
            "source_text",
            "characters",
            "location_id",
            "action",
            "camera",
            "lighting",
            "atmosphere",
            "duration",
            "start_state",
            "end_state",
        }
        if prompt_fields.intersection(changes):
            scene.visual_prompt = make_visual_prompt(
                action=scene.action,
                characters=characters,
                location=location,
                camera=scene.camera,
                lighting=scene.lighting,
                atmosphere=scene.atmosphere,
                style=project.visual_style,
                start_state=scene.start_state,
                end_state=scene.end_state,
            )
            scene.flow_prompt = make_flow_prompt(
                scene,
                characters=characters,
                location=location,
                visual_style=project.visual_style,
                previous_scene_id=project.scenes[scene_index - 1].id if scene_index else None,
            )
        if scene_index + 1 < len(project.scenes):
            affected = project.scenes[scene_index + 1 : min(len(project.scenes), scene_index + 4)]
            for downstream in affected:
                note = f"Thay đổi ở {scene.id} có thể ảnh hưởng continuity của {downstream.id}."
                if note not in downstream.warnings:
                    downstream.warnings.append(note)
        if protected:
            project.final_video = FinalVideo(status="NotReady")
        project = check_project(project, auto_fix=False)
        return self.storage.save(project)

    def set_scene_lock(self, project_id: str, scene_id: str, locked: bool) -> Project:
        project = self.get_required(project_id)
        scene = next((item for item in project.scenes if item.id == scene_id), None)
        if not scene:
            raise KeyError(scene_id)
        scene.ai_locked = locked
        scene.ai_lock_reason = (
            "AI đã duyệt dữ liệu scene và continuity" if locked else "Người dùng đã mở khóa"
        )
        return self.storage.save(project)

    def reorder(self, project_id: str, request: ReorderRequest) -> Project:
        project = self.get_required(project_id)
        current = {scene.id: scene for scene in project.scenes}
        requested = request.scene_ids
        if (
            len(requested) != len(current)
            or len(set(requested)) != len(requested)
            or set(requested) != set(current)
        ):
            raise ValueError("Reorder must contain every scene exactly once")
        project.scenes = [deepcopy(current[scene_id]) for scene_id in request.scene_ids]
        project = check_project(project, auto_fix=project.settings.auto_continuity)
        if all(scene.status == "Completed" and scene.result_file for scene in project.scenes):
            project.final_video = FinalVideo(status="Ready", scene_count=len(project.scenes))
        else:
            project.final_video = FinalVideo(status="NotReady")
        return self.storage.save(project)

    def check_continuity(self, project_id: str, auto_fix: bool | None = None) -> Project:
        project = self.get_required(project_id)
        use_auto_fix = project.settings.auto_continuity if auto_fix is None else auto_fix
        return self.storage.save(check_project(project, auto_fix=use_auto_fix))
