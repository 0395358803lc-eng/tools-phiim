"""Embedded Google Flow CLI integration for the Windows desktop application.

The former single-file monolith is now split into focused modules:

- ``errors.py``    shared error types
- ``catalog.py``   video model catalog
- ``discovery.py`` Flow CLI detection
- ``session.py``   cookie vault + connection status
- ``browser.py``   Chrome/CDP attach
- ``ui_compat.py`` Flow UI monkey-patching
- ``recovery.py``  download and job recovery
- ``generation.py`` generation orchestration

``FlowCLIIntegration`` below remains the single public entry point and keeps
its historical method surface so callers and tests are unaffected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..flow_credentials import CookieVault
from ..models import FlowConnection, Project, Scene
from ..providers.base import RenderResult
from . import generation, recovery
from . import session as session_module
from .browser import can_attach_existing_chrome
from .catalog import VIDEO_MODELS
from .errors import FlowIntegrationError, RenderCheckpoint
from .session import FlowSession
from .ui_compat import apply_flow_ui_compatibility

logger = logging.getLogger(__name__)

__all__ = [
    "FlowIntegrationError",
    "RenderCheckpoint",
    "FlowCLIIntegration",
    "VIDEO_MODELS",
]


class FlowCLIIntegration:
    def __init__(
        self,
        data_root: Path,
        timeout: int | None = None,
        credential_root: Path | None = None,
    ) -> None:
        self.data_root = data_root.resolve()
        self.session = FlowSession(data_root, credential_root=credential_root)
        self.timeout = timeout or int(os.getenv("FLOW_RENDER_TIMEOUT", "900"))
        self._force_headed_browser = False
        self._active_media_type = "video"
        self._chrome_port_file = Path(
            os.getenv(
                "FLOW_CHROME_DEVTOOLS_ACTIVE_PORT",
                str(
                    Path.home()
                    / "AppData"
                    / "Local"
                    / "Google"
                    / "Chrome"
                    / "User Data"
                    / "DevToolsActivePort"
                ),
            )
        )

    @property
    def configured(self) -> bool:
        return self.session.configured

    @property
    def vault(self) -> CookieVault:
        return self.session.vault

    def _clear_cookies(self) -> None:
        self.session._clear_cookies()

    def _save_cookies(
        self, cookies: dict[str, str], raw: list[dict[str, Any]] | None
    ) -> None:
        self.session._save_cookies(cookies, raw)

    def _load_cookies(self) -> tuple[dict[str, str], list[dict[str, Any]] | None]:
        return self.session._load_cookies()

    @staticmethod
    def _browser_ready() -> bool:
        return session_module._browser_ready()

    async def status(self, *, verify: bool = False) -> FlowConnection:
        return await self.session.status(verify=verify)

    async def connect(self, cookie_input: str) -> FlowConnection:
        return await self.session.connect(cookie_input)

    def disconnect(self) -> None:
        self.session.disconnect()

    def _client(self, cookies: dict[str, str], project_id: str | None = None) -> Any:
        self._apply_flow_ui_compatibility()
        from flow_cli._client import FlowClient

        client = FlowClient(cookies=cookies, project_id=project_id, timeout=30)
        _, raw = self.vault.load()
        if raw:
            client.raw_cookies = raw
        return client

    def _apply_flow_ui_compatibility(self) -> None:
        apply_flow_ui_compatibility(self)

    @staticmethod
    def _prompt(scene: Scene) -> str:
        return generation._prompt(scene)

    def _reference_path(self, value: str) -> str | None:
        return generation._reference_path(self, value)

    def _can_attach_existing_chrome(self) -> bool:
        return can_attach_existing_chrome(self._chrome_port_file)

    async def _generate_with_existing_chrome(
        self,
        client: Any,
        *,
        media_type: str,
        prompt: str,
        aspect: str,
        model: str,
        duration: int = 8,
        image_path: str | None = None,
        timeout: int,
        count: int = 1,
    ) -> Any:
        return await generation._generate_with_existing_chrome(
            self,
            client,
            media_type=media_type,
            prompt=prompt,
            aspect=aspect,
            model=model,
            duration=duration,
            image_path=image_path,
            timeout=timeout,
            count=count,
        )

    async def _generate_video(self, client: Any, **kwargs: Any) -> Any:
        return await generation._generate_video(self, client, **kwargs)

    @staticmethod
    def _job_identifiers(job: Any) -> set[str]:
        from ..flow_helpers import job_identifiers

        return job_identifiers(job)

    @staticmethod
    def _select_video_candidate(
        candidates: list[dict[str, str]], identifiers: set[str]
    ) -> dict[str, str] | None:
        from ..flow_helpers import select_video_candidate

        return select_video_candidate(candidates, identifiers)

    async def _download_via_browser(
        self,
        upstream_project_id: str,
        job: Any,
        output: Path,
    ) -> list[Path]:
        return await recovery._download_via_browser(
            self, upstream_project_id, job, output
        )

    async def _download_completed(
        self,
        client: Any,
        completed: Any,
        output: Path,
        upstream_project_id: str,
    ) -> list[Path]:
        return await recovery._download_completed(
            self,
            client,
            completed,
            output,
            upstream_project_id,
        )

    async def _recover_submitted(
        self,
        project: Project,
        scene: Scene,
    ) -> RenderResult | None:
        return await recovery._recover_submitted(self, project, scene)

    async def generate_reference_image(
        self, project_id: str, reference_id: str, prompt: str
    ) -> str:
        return await generation.generate_reference_image(
            self, project_id, reference_id, prompt
        )

    async def generate(
        self,
        project: Project,
        scene: Scene,
        checkpoint: RenderCheckpoint | None = None,
    ) -> RenderResult:
        return await generation.generate(self, project, scene, checkpoint)

    async def _extract_last_frame(
        self, project_id: str, scene_id: str, video: Path
    ) -> str:
        return await generation._extract_last_frame(self, project_id, scene_id, video)

    @staticmethod
    def _ffmpeg_path() -> str | None:
        return generation._ffmpeg_path()