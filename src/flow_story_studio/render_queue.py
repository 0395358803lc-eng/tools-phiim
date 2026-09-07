"""Sequential dependency-aware render queue with post-render production acceptance."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from .analysis_providers.xkiro import XKiroClient
from .audio_qc import AudioQCAnalyzer, can_normalize_audio, normalize_scene_audio
from .engines.continuity import is_direct_continuation
from .engines.quality import score_scene
from .film.image_plan import compile_project_image_plans
from .film.state_delta import (
    clear_accepted_runtime_state,
    commit_accepted_runtime_state,
    continuity_state_hash,
)
from .logging_config import get_logger
from .models import (
    AudioQCReport,
    ContinuityQCReport,
    FinalVideo,
    ProductionAcceptance,
    Project,
    Scene,
    VisualIssue,
    VisualQCReport,
)
from .production_gate import (
    is_scene_production_ready,
    project_master_blockers,
    scene_production_score_floor,
)
from .providers.base import VideoProvider
from .providers.reference import ReferenceProvider
from .providers.registry import ProviderRegistry, build_default_registry
from .providers.unavailable import RenderProviderUnavailable, UnavailableProvider
from .qc_repair import build_repair_instruction, can_auto_retry
from .reference_manager import ReferenceManager
from .scene_contracts import verify_scene_contract
from .storage import ProjectStorage
from .visual_qc import VisualQCAnalyzer

LOGGER = get_logger("render-queue")


class RenderQueue:
    def __init__(
        self,
        storage: ProjectStorage,
        xkiro: XKiroClient | None = None,
        data_root: Path | None = None,
        provider_registry: ProviderRegistry | None = None,
        reference_provider: ReferenceProvider | None = None,
    ) -> None:
        self.storage = storage
        self.providers = provider_registry or build_default_registry()
        self.data_root = (data_root or storage.root.parent).resolve()
        self.audio = AudioQCAnalyzer(self.data_root)
        self.vision = VisualQCAnalyzer(self.data_root, xkiro) if xkiro else None
        self.references: ReferenceManager | None = (
            ReferenceManager(reference_provider, self.vision, self.data_root)
            if self.vision
            else None
        )
        self._queues: dict[str, asyncio.Queue[str]] = defaultdict(asyncio.Queue)
        self._queued_ids: dict[str, set[str]] = defaultdict(set)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._resume_events: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _get_provider(self, project: Project) -> VideoProvider:
        return self.providers.get(project.settings.provider) or UnavailableProvider()

    def is_provider_configured(self, provider_name: str) -> bool:
        return provider_name in self.providers.configured_names()

    def _event(self, project_id: str) -> asyncio.Event:
        if project_id not in self._resume_events:
            event = asyncio.Event()
            event.set()
            self._resume_events[project_id] = event
        return self._resume_events[project_id]

    @staticmethod
    def _is_finally_accepted(project: Project, scene: Scene) -> bool:
        return is_scene_production_ready(project, scene)

    @classmethod
    def _refresh_final_video(cls, project: Project) -> None:
        all_accepted = all(
            cls._is_finally_accepted(project, item) for item in project.scenes
        )
        if project.scenes and all_accepted:
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
        provider = self.providers.get(project.settings.provider)
        master_blockers = (
            project_master_blockers(project, data_root=self.data_root)
            if bool(getattr(provider, "requires_master_gate", False))
            else []
        )
        if master_blockers:
            detail = " | ".join(master_blockers[:6])
            if len(master_blockers) > 6:
                detail += f" | +{len(master_blockers) - 6} lỗi Master khác"
            raise ValueError(
                "MASTER_GATE_BLOCKED: Không thể enqueue production trước khi toàn bộ "
                f"Project Masters đạt chuẩn. {detail}"
            )
        valid_ids = {scene.id for scene in project.scenes}
        requested = scene_ids or [
            scene.id for scene in project.scenes if scene.status != "Accepted"
        ]
        unknown = set(requested) - valid_ids
        if unknown:
            raise ValueError(f"Scene không tồn tại: {', '.join(sorted(unknown))}")
        blocked = [
            scene.id
            for scene in project.scenes
            if scene.id in requested and scene.image_plan.status == "Blocked"
        ]
        if blocked:
            raise ValueError(
                "Không thể enqueue render: Scene Image Plan đang Blocked vì thiếu "
                f"Master References được duyệt: {', '.join(blocked)}"
            )
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
                scene.render_provider = ""
                scene.render_model = ""
                clear_accepted_runtime_state(scene)
                scene.visual_qc = VisualQCReport()
                scene.audio_qc = AudioQCReport()
                scene.continuity_qc = ContinuityQCReport()
                scene.acceptance = ProductionAcceptance()
                if force_rerender:
                    scene.provider_job_id = ""
                    scene.upstream_project_id = ""
                    scene.upstream_workflow_id = ""
                    scene.upstream_media_id = ""
                    scene.upstream_resource_name = ""
                    scene.render_attempt = 0
                    scene.runtime_repair_instruction = ""
                    scene.repair_history = []
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

    def _dependency_predecessor(self, project: Project, scene: Scene) -> Scene | None:
        if scene.visual_plan.dependency_mode != "direct" or scene.order <= 1:
            return None
        previous = next(
            (item for item in project.scenes if item.order == scene.order - 1),
            None,
        )
        if not previous or not is_direct_continuation(previous, scene):
            return None
        return previous

    def _dependency_should_defer(self, project: Project, scene: Scene) -> bool:
        previous = self._dependency_predecessor(project, scene)
        if previous is None or self._is_finally_accepted(project, previous):
            return False
        return (
            previous.id in self._queued_ids[project.id]
            and previous.status in {"Waiting", "Preparing", "Generating", "QC", "Paused"}
        )

    def _dependency_block_reason(self, project: Project, scene: Scene) -> str:
        previous = self._dependency_predecessor(project, scene)
        if previous is None:
            return ""
        if not self._is_finally_accepted(project, previous):
            return f"Phụ thuộc scene {previous.order} chưa được Accepted"
        if previous.accepted_end_state is None or not previous.accepted_state_hash:
            return f"Scene {previous.order} chưa commit accepted state"
        if continuity_state_hash(previous.accepted_end_state) != previous.accepted_state_hash:
            return f"Accepted state hash của scene {previous.order} không hợp lệ"
        if continuity_state_hash(scene.start_state) != previous.accepted_state_hash:
            return (
                f"Start state của scene {scene.order} không khớp accepted state "
                f"scene {previous.order}"
            )
        return ""

    async def _prepare_reference(self, project: Project, scene: Scene) -> bool:
        if not self.references:
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
        compile_project_image_plans(project)
        self.storage.save(project)
        return True

    async def _post_render_qc(self, project: Project, scene: Scene) -> None:
        scene.quality = score_scene(scene, project.settings.quality_threshold)
        scene.render_provider = project.settings.provider
        scene.render_model = project.settings.video_model
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
            scene.audio_qc = AudioQCReport(
                status="Passed",
                score=100,
                audio_present=True,
                sample_rate_hz=48_000,
                channels=2,
                integrated_lufs=-16.0,
                true_peak_db=-1.0,
                clipping_detected=False,
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

        scene.audio_qc = await self.audio.inspect_scene(project, scene)
        if can_normalize_audio(scene.audio_qc):
            normalized, detail = await normalize_scene_audio(self.data_root, scene)
            if normalized:
                scene.audio_qc = await self.audio.inspect_scene(project, scene)
            else:
                scene.audio_qc.issues.append(
                    VisualIssue(
                        code="AUDIO_NORMALIZATION_FAILED",
                        message=detail[:500],
                    )
                )

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
        if scene.audio_qc.status != "Passed":
            reasons.append(f"Audio QC: {scene.audio_qc.status}")
        if scene.continuity_qc.status not in {"Passed", "NotApplicable"}:
            reasons.append(f"Continuity QC: {scene.continuity_qc.status}")
        score = scene_production_score_floor(scene)
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
                requeued = False
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

                    if self._dependency_should_defer(project, scene):
                        scene.status = "Waiting"
                        await queue.put(scene_id)
                        requeued = True
                        continue

                    dependency_reason = self._dependency_block_reason(project, scene)
                    if dependency_reason:
                        scene.acceptance = ProductionAcceptance(
                            status="Blocked",
                            reasons=[dependency_reason],
                        )
                        scene.warnings.append(f"Blocked: {dependency_reason}")
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
                    scene.render_attempt += 1
                    self.storage.save(project)
                    await self._update(project, scene, "Generating", 35)
                    provider = self._get_provider(project)
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
                        project.provider_project_id = result.upstream_project_id
                        scene.upstream_project_id = result.upstream_project_id
                    if result.upstream_media_id:
                        scene.upstream_media_id = result.upstream_media_id
                    if result.upstream_resource_name:
                        scene.upstream_resource_name = result.upstream_resource_name
                    self.storage.save(project)
                    await self._update(project, scene, "QC", 80)
                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
                    await self._post_render_qc(project, scene)
                    accepted = scene.acceptance.status == "Accepted"
                    if accepted:
                        commit_accepted_runtime_state(scene)
                        scene.runtime_repair_instruction = ""
                    await self._update(
                        project,
                        scene,
                        "Accepted" if accepted else "FailedQC",
                        100 if accepted else 85,
                    )
                    if not accepted:
                        scene.warnings.append(
                            "Production QC: " + "; ".join(scene.acceptance.reasons)
                        )
                        repair_instruction = build_repair_instruction(scene)
                        if repair_instruction and can_auto_retry(scene):
                            scene.runtime_repair_instruction = repair_instruction
                            scene.repair_history.append(repair_instruction)
                            scene.provider_job_id = ""
                            scene.upstream_project_id = ""
                            scene.upstream_workflow_id = ""
                            scene.upstream_media_id = ""
                            scene.upstream_resource_name = ""
                            scene.result_url = ""
                            scene.result_file = ""
                            scene.last_frame_file = ""
                            scene.render_provider = ""
                            scene.render_model = ""
                            clear_accepted_runtime_state(scene)
                            scene.visual_qc = VisualQCReport()
                            scene.audio_qc = AudioQCReport()
                            scene.continuity_qc = ContinuityQCReport()
                            scene.acceptance = ProductionAcceptance(
                                status="Pending",
                                reasons=[
                                    f"Auto repair attempt {scene.render_attempt + 1} scheduled"
                                ],
                            )
                            await self._update(project, scene, "Waiting", 0)
                            await queue.put(scene_id)
                            requeued = True
                            continue
                        scene.warnings.append(
                            "Manual review required after QC failure or retry budget exhaustion"
                        )
                        self.storage.save(project)
                        continue

                    project = self.storage.get(project_id)
                    if not project:
                        return
                    scene = next(item for item in project.scenes if item.id == scene_id)
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
                            compile_project_image_plans(project)
                            self.storage.save(project)
                except RenderProviderUnavailable as exc:
                    project = self.storage.get(project_id)
                    if project:
                        scene = next((item for item in project.scenes if item.id == scene_id), None)
                        if scene:
                            reason = str(exc) or "No render provider configured"
                            scene.warnings.append(f"Blocked: {reason}")
                            scene.acceptance = ProductionAcceptance(
                                status="Blocked", reasons=[reason]
                            )
                            await self._update(project, scene, "Blocked", 0)
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
                    if not requeued:
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
