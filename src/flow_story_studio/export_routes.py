"""FastAPI routes for project exports."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse

from .models import Project


def build_export_router(*, required: Callable[[str], Project]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project_id}/export.json")
    async def export_json(project_id: str) -> Response:
        project = required(project_id)
        return Response(
            content=project.model_dump_json(indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{project.id}.json"'},
        )

    @router.get("/api/projects/{project_id}/flow-prompts.zip")
    async def export_prompts(project_id: str) -> StreamingResponse:
        project = required(project_id)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for scene in project.scenes:
                archive.writestr(f"{scene.id}_prompt.txt", scene.flow_prompt)
            archive.writestr(
                "project_prompts.json",
                json.dumps(
                    {scene.id: scene.flow_prompt for scene in project.scenes},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{project.id}_flow_prompts.zip"'
            },
        )

    return router
