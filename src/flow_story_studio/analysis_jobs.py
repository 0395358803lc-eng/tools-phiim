"""In-memory lifecycle helpers for long-running story analysis jobs."""

from __future__ import annotations

import asyncio
from typing import Any

from .models import utc_now


class AnalysisJobRegistry:
    """Bounded in-memory analysis job state and task registry."""

    TERMINAL = {"completed", "failed", "cancelled"}

    def __init__(self, *, max_jobs: int = 50, max_logs: int = 2_000) -> None:
        self.max_jobs = max_jobs
        self.max_logs = max_logs
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def add_log(self, job: dict[str, Any], message: str, level: str = "info") -> None:
        logs = job.setdefault("logs", [])
        if not isinstance(logs, list):
            return
        logs.append({"at": utc_now(), "level": level, "message": message})
        if len(logs) > self.max_logs:
            del logs[: len(logs) - self.max_logs]

    def prune(self) -> None:
        if len(self.jobs) < self.max_jobs:
            return
        removable = sorted(
            (job for job in self.jobs.values() if job.get("status") in self.TERMINAL),
            key=lambda item: str(item.get("updated_at", "")),
        )
        remove_count = max(0, len(self.jobs) - self.max_jobs + 1)
        for old in removable[:remove_count]:
            job_id = str(old.get("id", ""))
            self.jobs.pop(job_id, None)
            self.tasks.pop(job_id, None)

    @staticmethod
    def snapshot(job: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in job.items() if key != "task"}

    def register_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        self.tasks[job_id] = task
        task.add_done_callback(lambda _task, current_id=job_id: self.tasks.pop(current_id, None))

    async def cancel(self, job_id: str) -> None:
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        active = [task for task in self.tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
