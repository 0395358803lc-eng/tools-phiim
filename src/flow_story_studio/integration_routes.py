"""FastAPI routes for external AI/video integrations."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .analysis_providers.xkiro import XKiroClient, XKiroError
from .flow_integration import FlowCLIIntegration, FlowIntegrationError
from .models import (
    FlowConnection,
    FlowCookieConnectRequest,
    FlowVideoModel,
    XKiroConnection,
    XKiroConnectRequest,
    XKiroModel,
)


def build_integration_router(
    flow: FlowCLIIntegration,
    xkiro: XKiroClient,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/video/flow/status", response_model=FlowConnection)
    async def flow_status(verify: bool = Query(default=False)) -> FlowConnection:
        try:
            return await flow.status(verify=verify)
        except FlowIntegrationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/api/video/flow/models", response_model=list[FlowVideoModel])
    async def flow_models() -> list[FlowVideoModel]:
        return list((await flow.status()).models)

    @router.post("/api/video/flow/connect", response_model=FlowConnection)
    async def flow_connect(request: FlowCookieConnectRequest) -> FlowConnection:
        try:
            return await flow.connect(request.cookie)
        except FlowIntegrationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @router.delete("/api/video/flow", response_model=FlowConnection)
    async def flow_disconnect() -> FlowConnection:
        try:
            flow.disconnect()
            return await flow.status()
        except FlowIntegrationError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @router.get("/api/ai/xkiro/status", response_model=XKiroConnection)
    async def xkiro_status(include_models: bool = Query(default=False)) -> XKiroConnection:
        try:
            return await xkiro.status(include_models=include_models)
        except XKiroError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/api/ai/xkiro/models", response_model=list[XKiroModel])
    async def xkiro_models(
        free_only: bool = Query(default=False), refresh: bool = Query(default=False)
    ) -> list[XKiroModel]:
        try:
            return await xkiro.list_models(free_only=free_only, refresh=refresh)
        except XKiroError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/api/ai/xkiro/connect", response_model=XKiroConnection)
    async def xkiro_connect(request: XKiroConnectRequest) -> XKiroConnection:
        try:
            return await xkiro.connect(request.api_key)
        except XKiroError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @router.delete("/api/ai/xkiro", response_model=XKiroConnection)
    async def xkiro_disconnect() -> XKiroConnection:
        try:
            xkiro.disconnect()
            return await xkiro.status()
        except XKiroError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
