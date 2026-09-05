"""Sequential dependency-aware render queue with post-render production acceptance."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from .analysis_providers.xkiro import XKiroClient
from .engines.continuity import is_direct_continuation
from .engines.quality import score_scene
from .flow_integration import FlowCLIIntegration
from .logging_config import get_logger
from .models import (
    ContinuityQCReport,
    FinalVideo,
    ProductionAcceptance,
    Project,
    Scene,
    VisualIssue,
    VisualQCReport,
)
from .providers.mock import MockProvider
from .reference_manager import ReferenceManager, promote_accepted_scene_references
from .scene_contracts import verify_scene_contract
from .storage import ProjectStorage
from .visual_qc import VisualQCAnalyzer

LOGGER = get_logger("render-queue")


class RenderQueue:
    def __init__(
        self,
        storage: ProjectStorage,
        flow: FlowCLIIntegration,
        xkiro: XKiroClient | None = None,
        data_root: Path | None = None,
    ) -> None:
        self.storage = storage
        self.flow = flow
        self.data_root = (data_root or storage.root.parent).resolve()
        self.vision = VisualQCAnalyzer(self.data_root, xkiro) if xkiro else None
        self.references = (
            ReferenceManager(flow, self.vision, self.data_root) if self.vision else None
        )
        self._queues: dict[str, asyncio.Queue[str]] = defaultdict(asyncio.Queue)
        self._queued_ids: dict[str, set[str]] = defaultdict(set)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._resume_events: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _event(self, project_id: str) -> asyncio.Event:
        if project_id not in self._resume_events:
            event = asyncio.Event()
            event.set()
            self._resume_events[project_id] = event
        return self._resume_events[project_id]

    @staticmethod
    def _is_finally_accepted(scene: Scene) -> bool:
        return (
            scene.status == "Accepted"
            and scene.acceptance.status == "Accepted"
            and scene.ai_locked
            and verify_scene_contract(scene)
            and bool(scene.result_file)
        )

    @classmethod
    def _refresh_final_video(cls, project: Project) -> None:
        if project.scenes and all(cls._is_finally_accepted(item) for item in project.scenes):
            project.final_video = FinalVideo(status="Ready", scene_count=len(project.scenes))
        elif project.final_video.status != "Merging":
            project.final_video = FinalVideo(status="NotReady")

    async def enqueue(
        self,
        project_id: str,
        scene_ids: list[str],
        *,
        force_rerender: bool = False,
    ) -> Project:
        project = self.storage.get(project_id)
        if not project:
            raise KeyError(project_id)
        valid_ids = {scene.id for scene in project.scenes}
        requested = scene_ids or [
            scene.id for scene in project.scenes if scene.status != "Accepted"
        ]
        unknown = set(requested) - valid_ids
        if unknown:
            raise ValueError(f"Scene không tồn tại: {', '.join(sorted(unknown))}")
        queued = self._queued_ids[project_id]
        worker = self._workers.get(project_id)
        worker_active = bool(worker and not worker.done())
        if requested:
            project.final_video = FinalVideo(status="NotReady")
        for scene in project.scenes:
            in_flight = scene.status in {"Preparing", "Generating", "QC"}
            if (
                scene.id in requested
                and scene.id not in queued
                and not (in_flight and worker_active)
            ):
                scene.status = "Waiting"
                scene.progress = 0
                scene.result_url = ""
                scene.result_file = ""
                scene.last_frame_file = ""
                scene.visual_qc = VisualQCReport()
                scene.continuity_qc = ContinuityQCReport()
                scene.acceptance = ProductionAcceptance()
                if force_rerender:
                    scene.provider_job_id = ""
                    scene.upstream_project_id = ""
                    scene.upstream_workflow_id = ""
                    scene.upstream_media_id = ""
                    scene.upstream_resource_name = ""
                scene.warnings = [
                    warning
                    for warning in scene.warnings
                    if not warning.startswith(("Render failed:", "Visual QC:", "Blocked:"))
                ]
                queued.add(scene.id)
                await self._queues[project_id].put(scene.id)
        self.storage.save(project)
        if not worker_active:
            self._workers[project_id] = asyncio.create_task(self._run(project_id))
        return project

    @staticmethod
    def _contract_block_reason(scene: Scene) -> str:
        if not scene.ai_locked:
            return "Scene chưa được AI Continuity Lock"
        if not verify_scene_contract(scene):
            return "Scene Packet contract không còn khớp dữ liệu đã seal"
        return ""

    def _dependency_blocker(self, project: Project, scene: Scene) -> Scene | None:
        if scene.visual_plan.dependency_mode != "direct" or scene.order <= 1:
            return None
        previous = next(
            (item for item in project.scenes if item.order == scene.order - 1),
            None,
        )
        if not previous or not is_direct_continuation(previous, scene):
            return None
        return None if self._is_finally_accepted(previous) else previous

    async def _prepare_reference(self, project: Project, scene: Scene) -> bool:
        if project.settings.provider != "google-flow":
            return True
        if not self.references:
            # Lightweight unit/provider harnesses may construct a queue without xKiro.
            # The desktop runtime always injects xKiro and therefore always has the
            # mandatory ReferenceManager gate below.
            return True
        approved = await self.references.ensure_scene_references(project, scene)
        if not approved:
            scene.acceptance = ProductionAcceptance(
                status="Blocked",
                reasons=["Canonical visual references are not approved yet."],
            )
            return False
        if not scene.reference_image:
            scene.reference_image = self.references.resolve_scene_reference(project, scene)
        self.storage.save(project)
        return True

    async def _post_render_qc(self, project: Project, scene: Scene) -> None:
        scene.quality = score_scene(scene, project.settings.quality_threshold)
        if project.settings.provider == "mock":
            scene.visual_qc = VisualQCReport(
                status="Passed",
                score=100,
                character_identity=100,
                location_identity=100,
                prop_consistency=100,
                wardrobe_consistency=100,
                lighting_consistency=100,
                action_consistency=100,
                composition_consistency=100,
                model_id="mock",
            )
            scene.continuity_qc = (
                ContinuityQCReport(status="Passed", score=100, model_id="mock")
                if scene.visual_plan.dependency_mode == "direct"
                else ContinuityQCReport(status="NotApplicable", score=100, model_id="mock")
            )
            scene.acceptance = ProductionAcceptance(status="Accepted", score=100)
            return

        if not scene.result_file:
            scene.visual_qc = VisualQCReport(
                status="Unavailable",
                issues=[
                    VisualIssue(code="VIDEO_FILE_MISSING", message="Không có MP4 để Visual QC")
                ],
            )
            scene.acceptance = ProductionAcceptance(
                status="Rejected", reasons=["Không có video file để nghiệm thu hình ảnh"]
            )
            return
        video = (self.data_root / scene.result_file).resolve()
        try:
            video.relative_to(self.data_root)
        except ValueError:
            scene.visual_qc = VisualQCReport(
                status="Unavailable",
                issues=[
                    VisualIssue(
                        code="VIDEO_PATH_INVALID", message="Video path nằm ngoài data root"
                    )
                ],
            )
            scene.acceptance = ProductionAcceptance(
                status="Rejected", reasons=["Video path không hợp lệ"]
            )
            return

        if not self.vision:
            scene.visual_qc.status = "Unavailable"
            scene.visual_qc.issues = [
                VisualIssue(code="VISION_NOT_CONFIGURED", message="xKiro Vision chưa khả dụng")
            ]
            scene.acceptance = ProductionAcceptance(
                status="Rejected", reasons=["Visual QC không khả dụng"]
            )
            return

        scene.visual_qc = await self.vision.inspect_scene(project, scene)
        scene.last_frame_file = scene.visual_qc.last_frame or scene.last_frame_file
        previous = next(
            (item for item in project.scenes if item.order == scene.order - 1),
            None,
        )
        if previous and scene.visual_plan.dependency_mode == "direct":
            scene.continuity_qc = await self.vision.inspect_continuity(project, previous, scene)
        else:
            scene.continuity_qc = ContinuityQCReport(status="NotApplicable", score=100)

        reasons: list[str] = []
        if not scene.quality or scene.quality.score < project.settings.quality_threshold:
            reasons.append("Preflight quality dưới ngưỡng")
        if scene.visual_qc.status != "Passed":
            reasons.append(f"Visual QC: {scene.visual_qc.status}")
        if scene.continuity_qc.status not in {"Passed", "NotApplicable"}:
            reasons.append(f"Continuity QC: {scene.continuity_qc.status}")
        components = [scene.visual_qc.score]
        if scene.continuity_qc.status != "NotApplicable":
            components.append(scene.continuity_qc.score)
        if scene.quality:
            components.append(scene.quality.score)
        score = round(sum(components) / len(components)) if components else 0
        scene.acceptance = ProductionAcceptance(
            status="Rejected" if reasons else "Accepted",
            score=score,
            reasons=reasons,
        )

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
                    contract_reason = self._contract_block_reason(scene)
                    if contract_reason:
                        scene.acceptance = ProductionAcceptance(
                            status="Blocked",
                            reasons=[contract_reason],
                        )
                        scene.warnings.append(f"Blocked: {contract_reason}")
                        await self._update(project, scene, "Blocked", 0)
                        continue

                    blocker = self._dependency_blocker(project, scene)
                    if blocker:
                        scene.acceptance = ProductionAcceptance(
                            status="Blocked",
                            reasons=[
                                f"Phụ thuộc scene {blocker.order} chưa được Accepted"
                            ],
                        )
                        scene.warnings.append(
                            f"Blocked: scene {blocker.order} chưa qua Production Acceptance"
                        )
                        await self._update(project, scene, "Blocked", 0)
                        continue

                    await self._update(project, scene, "Preparing", 10)
                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
                    references_ready = await self._prepare_reference(project, scene)
                    if not references_ready:
                        scene.warnings.append(
                            "Blocked: canonical visual references are not approved"
                        )
                        await self._update(project, scene, "Blocked", 0)
                        continue
                    await self._event(project_id).wait()
                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
                    await self._update(project, scene, "Generating", 35)
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
                    await self._update(project, scene, "QC", 80)
                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
                    await self._post_render_qc(project, scene)
                    accepted = scene.acceptance.status == "Accepted"
                    await self._update(
                        project,
                        scene,
                        "Accepted" if accepted else "FailedQC",
                        100 if accepted else 85,
                    )
                    if not accepted:
                        scene.warnings.append("Visual QC: " + "; ".join(scene.acceptance.reasons))
                        self.storage.save(project)
                        continue

                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
                    if promote_accepted_scene_references(project, scene):
                        self.storage.save(project)
                    if project.settings.auto_continuity and scene.visual_qc.last_frame:
                        next_scene = next(
                            (item for item in project.scenes if item.order == scene.order + 1),
                            None,
                        )
                        if (
                            next_scene
                            and next_scene.visual_plan.dependency_mode == "direct"
                            and is_direct_continuation(scene, next_scene)
                        ):
                            next_scene.reference_image = scene.visual_qc.last_frame
                            self.storage.save(project)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # queue containment boundary
                    LOGGER.exception("Render failed project=%s scene=%s", project_id, scene_id)
                    project = self.storage.get(project_id)
                    if project:
                        scene = next((item for item in project.scenes if item.id == scene_id), None)
                        if scene:
                            scene.warnings.append(f"Render failed: {type(exc).__name__}: {exc}")
                            scene.acceptance = ProductionAcceptance(
                                status="Rejected", reasons=[f"Render failed: {type(exc).__name__}"]
                            )
                            await self._update(project, scene, "Failed", 0)
                finally:
                    self._queued_ids[project_id].discard(scene_id)
                    queue.task_done()

    async def _update(self, project: Project, scene: Scene, status: str, progress: int) -> None:
        scene.status = status  # type: ignore[assignment]
        scene.progress = progress
        self._refresh_final_video(project)
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
