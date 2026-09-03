"""Sequential, continuity-aware render queue."""

from __future__ import annotations

import asyncio
from collections import defaultdict

from .engines.quality import score_scene
from .flow_integration import FlowCLIIntegration
from .models import FinalVideo, Project, Scene
from .providers.mock import MockProvider
from .storage import ProjectStorage


class RenderQueue:
    def __init__(self, storage: ProjectStorage, flow: FlowCLIIntegration) -> None:
        self.storage = storage
        self.flow = flow
        self._queues: dict[str, asyncio.Queue[str]] = defaultdict(asyncio.Queue)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._resume_events: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _event(self, project_id: str) -> asyncio.Event:
        if project_id not in self._resume_events:
            event = asyncio.Event()
            event.set()
            self._resume_events[project_id] = event
        return self._resume_events[project_id]

    async def enqueue(self, project_id: str, scene_ids: list[str]) -> Project:
        project = self.storage.get(project_id)
        if not project:
            raise KeyError(project_id)
        valid_ids = {scene.id for scene in project.scenes}
        requested = scene_ids or [
            scene.id for scene in project.scenes if scene.status != "Completed"
        ]
        unknown = set(requested) - valid_ids
        if unknown:
            raise ValueError(f"Scene không tồn tại: {', '.join(sorted(unknown))}")
        queued = set(self._queues[project_id]._queue)  # type: ignore[attr-defined]
        worker = self._workers.get(project_id)
        worker_active = bool(worker and not worker.done())
        if requested:
            project.final_video = FinalVideo(status="NotReady")
        for scene in project.scenes:
            in_flight = scene.status in {"Preparing", "Generating"}
            if (
                scene.id in requested
                and scene.id not in queued
                and not (in_flight and worker_active)
            ):
                scene.status = "Waiting"
                scene.progress = 0
                scene.result_url = ""
                scene.result_file = ""
                scene.warnings = [
                    warning
                    for warning in scene.warnings
                    if not warning.startswith("Render failed:")
                ]
                await self._queues[project_id].put(scene.id)
        self.storage.save(project)
        if not worker_active:
            self._workers[project_id] = asyncio.create_task(self._run(project_id))
        return project

    async def _run(self, project_id: str) -> None:
        queue = self._queues[project_id]
        async with self._locks[project_id]:
            while not queue.empty():
                scene_id = await queue.get()
                try:
                    await self._event(project_id).wait()
                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next((item for item in project.scenes if item.id == scene_id), None)
                    if not scene:
                        continue
                    await self._update(project, scene, "Preparing", 15)
                    await asyncio.sleep(0.12)
                    await self._event(project_id).wait()
                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
                    await self._update(project, scene, "Generating", 45)
                    provider = (
                        self.flow if project.settings.provider == "google-flow" else MockProvider()
                    )
                    if project.settings.provider == "google-flow":
                        result = await self.flow.generate(
                            project,
                            scene,
                            checkpoint=lambda current, _: self.storage.save(current),
                        )
                    else:
                        result = await provider.generate(project, scene)
                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
                    scene.provider_job_id = result.job_id
                    scene.result_url = result.result_url
                    scene.result_file = result.result_file
                    scene.last_frame_file = result.last_frame_file
                    if result.upstream_project_id:
                        project.flow_project_id = result.upstream_project_id
                    if project.settings.auto_continuity and result.last_frame_file:
                        next_scene = next(
                            (item for item in project.scenes if item.order == scene.order + 1),
                            None,
                        )
                        if next_scene and not next_scene.reference_image:
                            next_scene.reference_image = result.last_frame_file
                    scene.quality = score_scene(scene, project.settings.quality_threshold)
                    await self._update(project, scene, "Completed", 100)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # queue containment boundary
                    project = self.storage.get(project_id)
                    if project:
                        scene = next((item for item in project.scenes if item.id == scene_id), None)
                        if scene:
                            scene.warnings.append(f"Render failed: {type(exc).__name__}: {exc}")
                            await self._update(project, scene, "Failed", 0)
                finally:
                    queue.task_done()

    async def _update(self, project: Project, scene: Scene, status: str, progress: int) -> None:
        scene.status = status  # type: ignore[assignment]
        scene.progress = progress
        if all(item.status == "Completed" and bool(item.result_file) for item in project.scenes):
            project.final_video = FinalVideo(status="Ready", scene_count=len(project.scenes))
        elif project.final_video.status != "Merging":
            project.final_video = FinalVideo(status="NotReady")
        self.storage.save(project)

    def pause(self, project_id: str) -> None:
        self._event(project_id).clear()
        project = self.storage.get(project_id)
        if project:
            for scene in project.scenes:
                if scene.status == "Waiting":
                    scene.status = "Paused"
            self.storage.save(project)

    def resume(self, project_id: str) -> None:
        project = self.storage.get(project_id)
        if project:
            for scene in project.scenes:
                if scene.status == "Paused":
                    scene.status = "Waiting"
            self.storage.save(project)
        self._event(project_id).set()

    async def shutdown(self) -> None:
        tasks = [task for task in self._workers.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
