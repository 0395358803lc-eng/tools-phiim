"""Deterministic demo provider used when Google Flow is not configured."""

from __future__ import annotations

import asyncio
import uuid

from ..models import Project, Scene
from .base import RenderResult


class MockProvider:
    async def health(self) -> dict[str, object]:
        return {"ok": True, "provider": "mock", "message": "Chế độ mô phỏng sẵn sàng"}

    async def generate(self, project: Project, scene: Scene) -> RenderResult:
        await asyncio.sleep(0.35)
        return RenderResult(
            job_id=f"mock-{uuid.uuid4().hex[:10]}",
            result_url=f"mock://{project.id}/{scene.id}",
        )
