"""FastAPI routes for long-running story analysis jobs."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query

from .analysis_jobs import AnalysisJobRegistry
from .analysis_providers.xkiro import XKiroClient, XKiroError
from .models import AnalyzeRequest, utc_now
from .render_queue import RenderQueue
from .service import StudioService


def build_analysis_router(
    service: StudioService,
    xkiro: XKiroClient,
    queue: RenderQueue,
    registry: AnalysisJobRegistry,
) -> APIRouter:
    router = APIRouter()
    jobs = registry.jobs
    tasks = registry.tasks

    @router.post("/api/analysis/jobs", status_code=202)
    async def start_analysis_job(
        request: AnalyzeRequest, auto_pipeline: bool = Query(default=False)
    ) -> dict[str, object]:
        registry.prune()
        job_id = uuid4().hex[:16]
        job: dict[str, object] = {
            "id": job_id,
            "status": "queued",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "provider": request.settings.analysis_provider,
            "model": request.settings.analysis_model,
            "auto_pipeline": auto_pipeline,
            "logs": [],
            "project": None,
            "error": "",
        }
        jobs[job_id] = job
        registry.add_log(job, f"Đã tiếp nhận {len(request.original_text)} ký tự nội dung")

        async def run_job() -> None:
            job["status"] = "running"
            job["updated_at"] = utc_now()
            registry.add_log(
                job,
                f"Bắt đầu phân tích bằng {request.settings.analysis_provider}"
                + (
                    f" · {request.settings.analysis_model}"
                    if request.settings.analysis_model
                    else ""
                ),
            )
            started_at = asyncio.get_running_loop().time()

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(30)
                    if job.get("status") != "running":
                        return
                    elapsed = int(asyncio.get_running_loop().time() - started_at)
                    registry.add_log(
                        job,
                        f"Tác vụ dài hạn vẫn đang chạy · đã chờ {elapsed} giây; "
                        "tiến độ hoàn tất được lưu sau từng phần",
                    )
                    job["updated_at"] = utc_now()

            heartbeat_task = asyncio.create_task(heartbeat(), name=f"analysis-heartbeat-{job_id}")

            def progress(message: str, level: str = "info") -> None:
                registry.add_log(job, message, level)
                job["updated_at"] = utc_now()

            try:
                project = await service.analyze_with_provider(request, xkiro, progress)
                if auto_pipeline:
                    progress("Phân tích hoàn tất; đang đưa các cảnh vào hàng đợi video")
                    await queue.enqueue(project.id, [])
                job["project"] = {
                    "id": project.id,
                    "name": project.name,
                    "scene_count": len(project.scenes),
                    "continuity_score": project.continuity_score,
                    "scenes": [
                        {"id": scene.id, "status": scene.status, "progress": scene.progress}
                        for scene in project.scenes
                    ],
                }
                job["status"] = "completed"
                registry.add_log(
                    job,
                    f"Đã lưu dự án {project.name}: {len(project.scenes)} cảnh, "
                    f"continuity {project.continuity_score}%",
                    "success",
                )
            except asyncio.CancelledError:
                job["status"] = "cancelled"
                job["error"] = "Đã hủy phân tích"
                registry.add_log(job, "Người dùng đã hủy tác vụ phân tích", "warning")
                raise
            except XKiroError as exc:
                job["status"] = "failed"
                job["error"] = str(exc)
                registry.add_log(job, str(exc), "error")
            except Exception as exc:
                job["status"] = "failed"
                job["error"] = "Phân tích thất bại do lỗi nội bộ"
                registry.add_log(job, f"{type(exc).__name__}: {exc}", "error")
            finally:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                job["updated_at"] = utc_now()

        task = asyncio.create_task(run_job(), name=f"analysis-{job_id}")
        tasks[job_id] = task
        task.add_done_callback(lambda _task, current_id=job_id: tasks.pop(current_id, None))
        return registry.snapshot(job)

    @router.get("/api/analysis/jobs/{job_id}")
    async def get_analysis_job(job_id: str) -> dict[str, object]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ phân tích")
        return registry.snapshot(job)

    @router.delete("/api/analysis/jobs/{job_id}")
    async def cancel_analysis_job(job_id: str) -> dict[str, object]:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ phân tích")
        task = tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return registry.snapshot(job)

    return router
