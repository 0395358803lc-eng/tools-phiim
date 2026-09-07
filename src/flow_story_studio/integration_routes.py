"""FastAPI routes for external AI integration and neutral render status."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .analysis_providers.xkiro import XKiroClient, XKiroError
from .models import XKiroConnection, XKiroConnectRequest, XKiroModel
from .providers.registry import ProviderRegistry


def build_integration_router(xkiro: XKiroClient, providers: ProviderRegistry) -> APIRouter:
    router = APIRouter()

    @router.get("/api/render/status")
    async def render_status() -> dict[str, object]:
        configured = providers.configured_names()
        production = [name for name in configured if name != "mock"]
        details: dict[str, dict[str, object]] = {}
        for name in providers.names():
            if name == "unconfigured":
                details[name] = {
                    "configured": False,
                    "provider": name,
                    "message": "Chưa cấu hình render backend.",
                }
                continue
            provider = providers.get(name)
            if provider is None:
                continue
            try:
                health = await provider.health()
            except Exception as exc:
                health = {
                    "ok": False,
                    "configured": False,
                    "provider": name,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            details[name] = dict(health)
        return {
            "provider": production[0] if production else "unconfigured",
            "configured": bool(production),
            "message": (
                "Render backend đã sẵn sàng."
                if production
                else "Chưa có render backend production sẵn sàng."
            ),
            "available_providers": providers.names(),
            "configured_providers": configured,
            "provider_details": details,
        }

    @router.get("/api/ai/xkiro/status", response_model=XKiroConnection)
    async def xkiro_status(include_models: bool = Query(default=False)) -> XKiroConnection:
        try:
            return await xkiro.status(include_models=include_models)
        except XKiroError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/api/ai/xkiro/models", response_model=list[XKiroModel])
    async def xkiro_models(
        free_only: bool = Query(default=False),
        vision_only: bool = Query(default=False),
        refresh: bool = Query(default=False),
    ) -> list[XKiroModel]:
        try:
            models = await xkiro.list_models(free_only=free_only, refresh=refresh)
            if vision_only:
                models = [item for item in models if bool(item.capabilities.get("vision"))]
            return models
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
