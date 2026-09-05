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

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ..flow_credentials import CookieVault
from ..models import FlowConnection, Project, Scene
from ..providers.base import RenderResult
from . import generation, recovery
from . import session as session_module
from .browser import (
    can_attach_existing_chrome,
    default_flow_chrome_profile,
    inspect_live_flow_session,
    launch_flow_chrome,
)
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

        from .gflow_transport import gflow_available, gflow_enabled

        use_gflow = gflow_enabled() and gflow_available()
        explicit_profile = os.getenv("FLOW_CHROME_PROFILE", "").strip()
        if explicit_profile:
            default_profile = Path(explicit_profile).expanduser().resolve()
        elif use_gflow:
            try:
                from gflow_cli.auth import profile_dir as gflow_profile_dir

                profile_name = os.getenv("GFLOW_CLI_PROFILE", "").strip() or "default"
                default_profile = Path(gflow_profile_dir(profile_name)).resolve()
            except Exception:  # noqa: BLE001
                default_profile = default_flow_chrome_profile()
        else:
            default_profile = default_flow_chrome_profile()
        self._flow_chrome_profile = default_profile

        explicit_port = os.getenv("FLOW_CHROME_DEVTOOLS_ACTIVE_PORT", "").strip()
        self._chrome_port_file = (
            Path(explicit_port).expanduser().resolve()
            if explicit_port
            else self._flow_chrome_profile / "DevToolsActivePort"
        )
        # In gflow mode, never silently attach the user's unrelated Chrome
        # profile: login, verification and generation must share one profile.
        if (
            not use_gflow
            and not explicit_port
            and not can_attach_existing_chrome(self._chrome_port_file)
        ):
            local_app_data = Path(
                os.getenv(
                    "LOCALAPPDATA",
                    str(Path.home() / "AppData" / "Local"),
                )
            )
            user_chrome_port = (
                local_app_data
                / "Google"
                / "Chrome"
                / "User Data"
                / "DevToolsActivePort"
            ).resolve()
            if can_attach_existing_chrome(user_chrome_port):
                self._chrome_port_file = user_chrome_port

    @property
    def configured(self) -> bool:
        from .gflow_transport import (
            gflow_available,
            gflow_enabled,
            resolve_gflow_profile_dir,
        )

        if gflow_enabled() and gflow_available():
            try:
                resolve_gflow_profile_dir()
                return True
            except FlowIntegrationError:
                return False
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
        from .gflow_transport import (
            gflow_available,
            gflow_enabled,
            resolve_gflow_profile_dir,
            verify_gflow_profile,
        )

        cdp_ready = self._can_attach_existing_chrome()
        if gflow_enabled() and gflow_available():
            connection = await self.session.status(verify=False)
            connection.transport = "gflow+flow.google.com"
            connection.flow_cli_available = True
            connection.cdp_ready = cdp_ready
            connection.browser_ready = True
            connection.cookie_count = 0

            profile_ready = False
            try:
                resolve_gflow_profile_dir()
                profile_ready = True
            except FlowIntegrationError:
                profile_ready = False
            connection.configured = profile_ready

            live_project_id: str | None = None
            if verify and cdp_ready:
                try:
                    _cookies, _raw, live_authenticated, live_project_id = (
                        await inspect_live_flow_session(self._chrome_port_file)
                    )
                    connection.authenticated = live_authenticated
                    connection.configured = profile_ready or live_authenticated
                    if live_authenticated:
                        suffix = (
                            f" Project live: {live_project_id}."
                            if live_project_id
                            else ""
                        )
                        connection.message = (
                            "gflow profile đã xác thực trên Flow UI live."
                            + suffix
                            + " Đóng cửa sổ Chrome đăng nhập trước khi bắt đầu render "
                            "để gflow có thể mở profile độc quyền."
                        )
                    else:
                        connection.message = (
                            "Chrome gflow profile đang mở nhưng chưa xác nhận được "
                            "phiên Flow đã đăng nhập."
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not inspect live gflow profile: %s", exc)
                    connection.authenticated = False
                    connection.message = "Không thể xác minh live gflow profile."
            elif verify:
                authenticated, detail, _profile = await verify_gflow_profile()
                connection.authenticated = authenticated
                connection.configured = profile_ready
                connection.message = (
                    "gflow profile đã xác thực và sẵn sàng render."
                    if authenticated
                    else f"gflow profile chưa sẵn sàng: {detail}"
                )
            else:
                connection.authenticated = False
                if cdp_ready:
                    connection.message = (
                        "Chrome gflow profile đã mở; đăng nhập Flow rồi nhấn Kiểm tra."
                    )
                elif profile_ready:
                    connection.message = (
                        "Đã lưu gflow profile; nhấn Kiểm tra để xác thực phiên Flow."
                    )
                else:
                    connection.message = (
                        "gflow đã cài nhưng profile chưa có phiên Flow; "
                        "hãy mở phiên đăng nhập Flow."
                    )

            connection.interactive_login_required = not connection.authenticated
            return connection

        live_authenticated = False
        live_project_id: str | None = None
        if verify and cdp_ready:
            try:
                cookies, raw, live_authenticated, live_project_id = (
                    await inspect_live_flow_session(self._chrome_port_file)
                )
                if cookies:
                    self._save_cookies(cookies, raw)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Could not inspect the live Flow Chrome session: %s", exc
                )

        connection = await self.session.status(verify=verify and not cdp_ready)
        if verify and cdp_ready:
            connection.authenticated = live_authenticated
            connection.transport = "flow-cli+chrome-cdp"
            if live_authenticated:
                suffix = (
                    f" Project live: {live_project_id}."
                    if live_project_id
                    else ""
                )
                connection.message = (
                    "Phiên Google Flow live đã xác thực qua Chrome CDP; "
                    "không dùng endpoint tRPC labs.google đã lỗi thời."
                    + suffix
                )
            else:
                connection.message = (
                    "Chrome Flow profile đã mở nhưng chưa xác nhận được phiên "
                    "Flow đã đăng nhập."
                )
        connection.cdp_ready = cdp_ready
        connection.interactive_login_required = not connection.authenticated
        if cdp_ready and not verify and not connection.authenticated:
            connection.message = (
                "Chrome Flow profile đã mở; nhấn Kiểm tra để xác thực trực tiếp trên UI Flow."
            )
        return connection

    async def start_browser_session(self) -> FlowConnection:
        if not self._can_attach_existing_chrome():
            try:
                port_file = await asyncio.to_thread(
                    launch_flow_chrome,
                    self._flow_chrome_profile,
                )
            except RuntimeError as exc:
                raise FlowIntegrationError(str(exc)) from exc
            self._chrome_port_file = port_file
        connection = await self.status(verify=False)
        connection.cdp_ready = self._can_attach_existing_chrome()
        connection.interactive_login_required = not connection.authenticated
        if connection.cdp_ready and not connection.authenticated:
            connection.message = (
                "Chrome Flow profile đã mở; hãy đăng nhập Google Flow trong cửa sổ "
                "Chrome rồi nhấn Kiểm tra."
            )
        elif connection.cdp_ready:
            connection.message = "Chrome Flow profile đã xác thực và sẵn sàng cho CDP."
        return connection

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