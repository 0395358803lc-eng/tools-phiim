from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .browser_sessions import BrowserSessionError, GoogleFlowSessionManager


def build_browser_session_router(manager: GoogleFlowSessionManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/google-flow/session")
    async def google_flow_session_status() -> dict[str, object]:
        return manager.status()

    @router.post("/api/google-flow/session/new")
    async def google_flow_session_new() -> dict[str, object]:
        try:
            return manager.create_new()
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/google-flow/session/open")
    async def google_flow_session_open() -> dict[str, object]:
        try:
            return manager.open_active()
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/google-flow/session/pending/open")
    async def google_flow_pending_open() -> dict[str, object]:
        try:
            return manager.open_pending()
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/google-flow/session/pending/activate")
    async def google_flow_pending_activate() -> dict[str, object]:
        try:
            return manager.activate_pending()
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete("/api/google-flow/session/pending")
    async def google_flow_pending_cancel() -> dict[str, object]:
        return manager.cancel_pending()

    return router
