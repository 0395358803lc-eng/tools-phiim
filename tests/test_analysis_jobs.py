from __future__ import annotations

import asyncio

import pytest

from flow_story_studio.analysis_jobs import AnalysisJobRegistry


def _job(job_id: str, status: str, updated_at: str) -> dict[str, object]:
    return {
        "id": job_id,
        "status": status,
        "updated_at": updated_at,
        "logs": [],
    }


def test_registry_caps_logs() -> None:
    registry = AnalysisJobRegistry(max_logs=3)
    job = _job("job", "running", "2026-01-01T00:00:00Z")

    for index in range(5):
        registry.add_log(job, f"message-{index}")

    logs = job["logs"]
    assert isinstance(logs, list)
    assert [item["message"] for item in logs] == ["message-2", "message-3", "message-4"]


def test_registry_prunes_oldest_terminal_job() -> None:
    registry = AnalysisJobRegistry(max_jobs=2)
    registry.jobs["old"] = _job("old", "completed", "2026-01-01T00:00:00Z")
    registry.jobs["running"] = _job("running", "running", "2026-01-02T00:00:00Z")

    registry.prune()

    assert "old" not in registry.jobs
    assert "running" in registry.jobs


def test_registry_rejects_capacity_when_all_jobs_are_active() -> None:
    registry = AnalysisJobRegistry(max_jobs=2)
    registry.jobs["one"] = _job("one", "running", "2026-01-01T00:00:00Z")
    registry.jobs["two"] = _job("two", "queued", "2026-01-02T00:00:00Z")
    assert registry.prune() is False
    assert set(registry.jobs) == {"one", "two"}


def test_registry_snapshot_excludes_task_field() -> None:
    job = {"id": "job", "status": "queued", "task": object()}
    snapshot = AnalysisJobRegistry.snapshot(job)
    assert snapshot == {"id": "job", "status": "queued"}


@pytest.mark.asyncio
async def test_registry_removes_finished_task() -> None:
    registry = AnalysisJobRegistry()

    async def work() -> None:
        await asyncio.sleep(0)

    task = asyncio.create_task(work())
    registry.register_task("job", task)
    await task
    await asyncio.sleep(0)

    assert "job" not in registry.tasks
