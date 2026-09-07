"""Controlled provider used when no render backend is configured."""

from __future__ import annotations

from ..models import Project, Scene
from .base import RenderResult


class RenderProviderUnavailable(RuntimeError):
    pass


class UnavailableProvider:
    async def health(self) -> dict[str, object]:
        return {
            "ok": False,
            "provider": "unconfigured",
            "configured": False,
            "message": "Chưa cấu hình render provider.",
        }

    async def generate(self, project: Project, scene: Scene) -> RenderResult:
        raise RenderProviderUnavailable("Chưa có render provider được cấu hình.")
