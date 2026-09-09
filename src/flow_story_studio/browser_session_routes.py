from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from .browser_sessions import BrowserSessionError, GoogleFlowSessionManager


def build_browser_session_router(manager: GoogleFlowSessionManager) -> APIRouter:
    router = APIRouter()

    async def session_payload(request: Request) -> dict[str, object]:
        body = await request.body()
        if not body or len(body) > 2 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail="Session JSON phải từ 1 byte đến 2 MB",
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="Session JSON không hợp lệ") from exc
        if isinstance(payload, list):
            return {"cookies": payload}
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=422,
                detail="Session payload phải là JSON object hoặc danh sách cookie",
            )
        return payload

    @router.get("/api/google-flow/session")
    async def google_flow_session_status() -> dict[str, object]:
        return manager.status()

    @router.post("/api/google-flow/session/import")
    async def google_flow_session_import(request: Request) -> dict[str, object]:
        payload = await session_payload(request)
        try:
            return manager.import_session(payload)
        except BrowserSessionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/google-flow/session/google-account/import")
    async def google_account_session_import(request: Request) -> dict[str, object]:
        payload = await session_payload(request)
        try:
            return manager.import_google_account_session(payload)
        except BrowserSessionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/google-flow/session/flow/import")
    async def flow_app_session_import(request: Request) -> dict[str, object]:
        payload = await session_payload(request)
        try:
            return manager.import_flow_session(payload)
        except BrowserSessionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/api/google-flow/session/google-account")
    async def google_account_session_clear() -> dict[str, object]:
        try:
            return manager.clear_source_session("google_account")
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete("/api/google-flow/session/flow")
    async def flow_app_session_clear() -> dict[str, object]:
        try:
            return manager.clear_source_session("google_flow")
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/google-flow/session/proxy")
    async def google_flow_proxy_save(request: Request) -> dict[str, object]:
        body = await request.body()
        if not body or len(body) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Proxy config phải từ 1 byte đến 64 KB")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail="Proxy config JSON không hợp lệ") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Proxy config phải là JSON object")
        try:
            return manager.save_proxy(payload)
        except BrowserSessionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/api/google-flow/session/proxy")
    async def google_flow_proxy_clear() -> dict[str, object]:
        try:
            return manager.clear_proxy()
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/google-flow/session/validate")
    async def google_flow_session_validate() -> dict[str, object]:
        try:
            return await asyncio.to_thread(manager.validate_session)
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete("/api/google-flow/session")
    async def google_flow_session_clear() -> dict[str, object]:
        try:
            return manager.clear_session()
        except BrowserSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Legacy endpoints remain as controlled errors for old clients.
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
