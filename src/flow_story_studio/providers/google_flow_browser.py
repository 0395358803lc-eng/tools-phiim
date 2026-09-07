from __future__ import annotations

import asyncio
from pathlib import Path

from ..google_flow_browser_worker import GoogleFlowBrowserWorker
from ..models import Project, Scene
from .base import RenderResult


class GoogleFlowBrowserProvider:
    """Provider adapter backed by Google Flow's browser UI."""

    name = "google-flow-browser"
    requires_master_gate = True

    def __init__(self, worker: GoogleFlowBrowserWorker) -> None:
        self.worker = worker
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return self.worker.configured()

    async def health(self) -> dict[str, object]:
        return await asyncio.to_thread(self.worker.health)

    async def generate(self, project: Project, scene: Scene) -> RenderResult:
        async with self._lock:
            asset = await asyncio.to_thread(self.worker.generate_video, project, scene)
        project.provider_project_id = asset.project_id
        return RenderResult(
            job_id=f"google-flow:{asset.media_id}",
            result_url=f"/api/projects/{project.id}/scenes/{scene.id}/video",
            result_file=asset.result_file,
            upstream_project_id=asset.project_id,
            upstream_media_id=asset.media_id,
            upstream_resource_name=asset.label,
        )

    async def generate_reference_image(
        self,
        project: Project,
        reference_id: str,
        prompt: str,
        *,
        ingredient_files: list[Path] | None = None,
    ) -> str:
        image_model = project.settings.image_model or "Nano Banana 2"
        async with self._lock:
            asset = await asyncio.to_thread(
                self.worker.generate_image,
                project,
                prompt=prompt,
                output_token=reference_id,
                model=image_model,
                ingredient_files=ingredient_files,
            )
        project.provider_project_id = asset.project_id
        return asset.result_file

    async def generate_scene_images(
        self,
        project: Project,
        scene: Scene,
    ) -> dict[str, str]:
        plan = scene.image_plan
        if plan.status == "Blocked":
            raise RuntimeError(
                "Scene Image Plan đang Blocked; cần duyệt đủ Master References trước khi tạo ảnh"
            )

        master_paths = [
            (self.worker.data_root / relative).resolve()
            for relative in plan.approved_reference_images
        ]
        image_model = project.settings.image_model or "Nano Banana 2"
        generated_start = plan.generated_start_frame
        inherited_start = plan.start_frame_source

        async with self._lock:
            if plan.start_frame_strategy == "canonical_reanchor" and not generated_start:
                start_asset = await asyncio.to_thread(
                    self.worker.generate_image,
                    project,
                    prompt=plan.start_frame_prompt,
                    output_token=f"{scene.id}-start",
                    model=image_model,
                    ingredient_files=master_paths,
                )
                generated_start = start_asset.result_file
                project.provider_project_id = start_asset.project_id

            start_for_target = generated_start or inherited_start
            target_ingredients = list(master_paths)
            if start_for_target:
                start_path = (self.worker.data_root / start_for_target).resolve()
                if start_path not in target_ingredients:
                    target_ingredients.append(start_path)

            target_asset = await asyncio.to_thread(
                self.worker.generate_image,
                project,
                prompt=plan.target_frame_prompt,
                output_token=f"{scene.id}-target",
                model=image_model,
                ingredient_files=target_ingredients,
            )
            project.provider_project_id = target_asset.project_id

        return {
            "generated_start_frame": generated_start,
            "generated_target_frame": target_asset.result_file,
            "upstream_project_id": project.provider_project_id,
            "upstream_media_id": target_asset.media_id,
        }
