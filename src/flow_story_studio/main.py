"""TH Media FastAPI application."""

from __future__ import annotations

import asyncio
import hmac
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .analysis_jobs import AnalysisJobRegistry
from .analysis_providers.xkiro import XKiroClient
from .analysis_routes import build_analysis_router
from .browser_session_routes import build_browser_session_router
from .browser_sessions import GoogleFlowSessionManager
from .export_routes import build_export_router
from .google_flow_browser_worker import GoogleFlowBrowserWorker
from .integration_routes import build_integration_router
from .logging_config import get_logger
from .models import (
    FinalVideo,
    Project,
)
from .project_routes import build_project_router
from .providers.google_flow_browser import GoogleFlowBrowserProvider
from .providers.reference import ReferenceProvider
from .providers.registry import ProviderRegistry, build_default_registry
from .render_queue import RenderQueue
from .service import StudioService
from .storage import ProjectStorage
from .video_merger import VideoMerger
from .video_routes import build_video_router

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
STATIC_ROOT = RESOURCE_ROOT / "static"
DATA_ROOT = Path(os.getenv("FLOW_STUDIO_DATA_DIR", PROJECT_ROOT / "data"))
LOGGER = get_logger("api")


def create_app(
    storage: ProjectStorage | None = None,
    xkiro_client: XKiroClient | None = None,
    provider_registry: ProviderRegistry | None = None,
    reference_provider: ReferenceProvider | None = None,
    browser_session_manager: GoogleFlowSessionManager | None = None,
    credential_root: Path | None = None,
    session_token: str | None = None,
) -> FastAPI:
    project_storage = storage or ProjectStorage(DATA_ROOT / "projects")
    runtime_data_root = project_storage.root.parent
    service = StudioService(project_storage)
    credential_dir = (credential_root or runtime_data_root / "secrets").resolve()
    browser_sessions = browser_session_manager or GoogleFlowSessionManager(
        runtime_data_root / "browser-sessions" / "google-flow"
    )
    providers = provider_registry or build_default_registry()
    google_flow_provider: GoogleFlowBrowserProvider | None = None
    if provider_registry is None:
        google_flow_provider = GoogleFlowBrowserProvider(
            GoogleFlowBrowserWorker(browser_sessions, runtime_data_root)
        )
        providers.register(GoogleFlowBrowserProvider.name, google_flow_provider)
    xkiro = xkiro_client or XKiroClient(credential_path=credential_dir / "xkiro-api-key.bin")
    xkiro.set_checkpoint_root(runtime_data_root / "analysis-checkpoints")
    effective_reference_provider = reference_provider or google_flow_provider
    queue = RenderQueue(
        project_storage,
        xkiro=xkiro,
        data_root=runtime_data_root,
        provider_registry=providers,
        reference_provider=effective_reference_provider,
    )
    merger = VideoMerger(runtime_data_root)
    analysis_registry = AnalysisJobRegistry()
    analysis_jobs = analysis_registry.jobs
    analysis_tasks = analysis_registry.tasks
    merge_tasks: dict[str, asyncio.Task[None]] = {}
    session_id = os.getenv("FLOW_STUDIO_SESSION_ID") or uuid4().hex

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        for task in analysis_tasks.values():
            if not task.done():
                task.cancel()
        if analysis_tasks:
            await asyncio.gather(*analysis_tasks.values(), return_exceptions=True)
        for task in merge_tasks.values():
            if not task.done():
                task.cancel()
        if merge_tasks:
            await asyncio.gather(*merge_tasks.values(), return_exceptions=True)
        await queue.shutdown()

    app = FastAPI(
        title="TH Media",
        version=__version__,
        description="Continuity-first storyboard with provider-neutral render pipeline",
        lifespan=lifespan,
    )
    app.state.storage = project_storage
    app.state.service = service
    app.state.queue = queue
    app.state.merger = merger
    app.state.xkiro = xkiro
    app.state.providers = providers
    app.state.google_flow_provider = google_flow_provider
    app.state.reference_provider = effective_reference_provider
    app.state.browser_sessions = browser_sessions
    app.state.analysis_jobs = analysis_jobs
    app.state.session_auth_required = bool(session_token)

    @app.middleware("http")
    async def desktop_session_auth(request: Request, call_next):
        if session_token and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("x-flow-studio-session", "")
            if not hmac.compare_digest(supplied, session_token):
                return JSONResponse(status_code=401, content={"detail": "Invalid desktop session"})
        try:
            return await call_next(request)
        except Exception:
            LOGGER.exception(
                "Unhandled API error method=%s path=%s", request.method, request.url.path
            )
            raise

    def required(project_id: str) -> Project:
        try:
            project = service.get_required(project_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy project") from exc
        merge_task = merge_tasks.get(project_id)
        if project.final_video.status == "Merging" and not (merge_task and not merge_task.done()):
            ready = all(
                scene.status == "Accepted"
                and scene.acceptance.status == "Accepted"
                and bool(scene.result_file)
                for scene in project.scenes
            )
            project.final_video = FinalVideo(
                status="Ready" if ready else "NotReady",
                scene_count=len(project.scenes) if ready else 0,
            )
            project_storage.save(project)
        return project

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        configured = providers.configured_names()
        production = [name for name in configured if name != "mock"]
        return {
            "ok": True,
            "version": __version__,
            "analysis": {"offline": True, "xkiro_configured": xkiro.configured},
            "render": {
                "provider": production[0] if production else "unconfigured",
                "configured": bool(production),
            },
        }

    @app.get("/api/session")
    async def session_info() -> dict[str, object]:
        return {
            "id": session_id,
            "workspace": str(runtime_data_root.resolve()),
            "fresh_start": True,
            "session_auth_required": bool(session_token),
        }

    app.include_router(build_integration_router(xkiro, providers))
    app.include_router(build_browser_session_router(browser_sessions))

    app.include_router(build_analysis_router(service, xkiro, queue, analysis_registry))

    app.include_router(
        build_project_router(
            storage=project_storage,
            service=service,
            xkiro=xkiro,
            queue=queue,
            runtime_data_root=runtime_data_root,
            required=required,
        )
    )

    app.include_router(
        build_video_router(
            storage=project_storage,
            queue=queue,
            merger=merger,
            runtime_data_root=runtime_data_root,
            merge_tasks=merge_tasks,
            required=required,
        )
    )

    app.include_router(build_export_router(required=required))

    if STATIC_ROOT.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_ROOT), name="assets")

        @app.get("/", include_in_schema=False)
        async def index(_: Request) -> FileResponse:
            return FileResponse(STATIC_ROOT / "index.html")

    return app


app = create_app()


def run() -> None:
    uvicorn.run("flow_story_studio.main:app", host="127.0.0.1", port=8010, reload=False)


if __name__ == "__main__":
    run()
