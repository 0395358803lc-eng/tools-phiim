"""Provider-neutral render contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import Project, Scene


@dataclass(slots=True)
class RenderResult:
    job_id: str
    result_url: str
    result_file: str = ""
    last_frame_file: str = ""
    upstream_project_id: str = ""


class VideoProvider(Protocol):
    async def health(self) -> dict[str, object]: ...

    async def generate(self, project: Project, scene: Scene) -> RenderResult: ...
