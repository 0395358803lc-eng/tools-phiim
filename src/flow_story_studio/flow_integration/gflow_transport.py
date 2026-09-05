"""Modern gflow-cli transport for the current flow.google.com UI."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from ..flow_helpers import flow_prompt, reference_path
from ..models import Project, Scene
from ..providers.base import RenderResult
from .errors import FlowIntegrationError, RenderCheckpoint


def gflow_available() -> bool:
    """Return whether the current Python environment contains gflow-cli."""
    return importlib.util.find_spec("gflow_cli") is not None


def gflow_enabled() -> bool:
    """Prefer gflow for video unless an explicit legacy rollback is requested."""
    value = os.getenv("FLOW_VIDEO_TRANSPORT", "gflow").strip().casefold()
    return value not in {"legacy", "flow-cli", "flow_cli", "off", "0", "false"}


def resolve_gflow_profile_dir() -> Path:
    """Resolve the active gflow persistent Chrome profile without hard-coding its name."""
    if not gflow_available():
        raise FlowIntegrationError("gflow-cli chưa được cài trong môi trường ứng dụng")
    try:
        from gflow_cli import profile_store
        from gflow_cli.auth import profile_dir, status

        requested = os.getenv("GFLOW_CLI_PROFILE", "").strip()
        if requested:
            profile_name = requested
        else:
            default_path = Path(profile_dir("default")).resolve()
            profile_name = (
                "default"
                if default_path.is_dir()
                else profile_store.resolve_profile(None)
            )
        profile_path = Path(profile_dir(profile_name)).resolve()
        profile_status = status(profile_name)
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError(
            "gflow chưa có profile Google Flow khả dụng; cần đăng nhập profile gflow trước"
        ) from exc

    if not profile_path.is_dir() or not bool(profile_status.get("cookies_present")):
        raise FlowIntegrationError(
            "Profile gflow chưa có phiên Google Flow đã lưu; cần đăng nhập profile gflow trước"
        )
    return profile_path


def _map_video_model(model: str) -> Any:
    """Map the app model id to gflow's strongly typed VideoModel."""
    from gflow_cli.api.video import VideoModel

    aliases = {
        "veo-3.1-lite-lower-priority": "veo_lite_lp",
        "veo-3.1-lite": "veo_lite",
        "veo-3.1-fast": "veo_fast",
        "veo-3.1-quality": "veo_quality",
        "omni-1.1-flash": "omni_flash",
        "omni-flash": "omni_flash",
    }
    normalized = model.strip().casefold()
    return VideoModel.from_cli(aliases.get(normalized, normalized))


def _request_duration(model: Any, scene_duration: int) -> int | None:
    """Request duration only when the active Flow cohort can express it safely."""
    if scene_duration not in {4, 6, 8, 10}:
        return None

    legacy_supports = getattr(model, "supports_duration", None)
    if callable(legacy_supports):
        return scene_duration if legacy_supports() else None

    # Newer gflow versions correctly model Veo duration as cohort-dependent.
    # Do not force the control for Veo: some migrated Frames/T2V cohorts omit it,
    # and a forced duration would turn a valid generation into a pre-submit error.
    model_value = str(getattr(model, "value", model)).strip().casefold()
    if model_value == "omni_flash":
        return scene_duration
    return None


def _apply_migrated_ui_compat() -> None:
    """Patch narrow upstream selector drift for duplicated migrated settings triggers."""
    try:
        from gflow_cli.api.transports import migrated_composer
    except Exception:  # noqa: BLE001
        return

    # Flow can render two responsive copies of the settings trigger at once:
    # the first carries ``hidden`` while the second is visible. gflow <= 0.68.0
    # resolves ``.first`` and waits forever on the hidden copy. Restricting the
    # selector to the visible DOM branch preserves the upstream driver/wire logic.
    if getattr(migrated_composer, "READY_ANCHOR", "") == ".settings-trigger-button":
        migrated_composer.READY_ANCHOR = ".settings-trigger-button:not([hidden])"


def _validate_downloaded_mp4(path: Path) -> None:
    if not path.is_file():
        raise FlowIntegrationError("gflow báo hoàn tất nhưng không có tệp video tải xuống")
    if path.suffix.casefold() != ".mp4":
        raise FlowIntegrationError(
            f"gflow trả về tệp không phải MP4: {path.name}"
        )
    try:
        head = path.read_bytes()[:16]
    except OSError as exc:
        raise FlowIntegrationError("Không đọc được MP4 do gflow tải xuống") from exc
    if len(head) < 8 or head[4:8] != b"ftyp":
        raise FlowIntegrationError("Tệp gflow tải xuống không có chữ ký MP4 hợp lệ")


async def generate_video_with_gflow(
    integration: Any,
    project: Project,
    scene: Scene,
    *,
    safe_model: str,
    checkpoint: RenderCheckpoint | None = None,
) -> RenderResult:
    """Generate one scene through gflow's current flow.google.com browser transport."""
    try:
        from gflow_cli.api.client import FlowApiClient
        from gflow_cli.api.video import Aspect, GenerateVideoRequest, Mode
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError("Không thể nạp gflow-cli video transport") from exc

    _apply_migrated_ui_compat()
    profile_path = resolve_gflow_profile_dir()
    output = integration.data_root / "renders" / project.id / scene.id
    output.mkdir(parents=True, exist_ok=True)

    ref_value = reference_path(integration.data_root, scene.reference_image)
    start_image = Path(ref_value).resolve() if ref_value else None
    if start_image is not None and not start_image.is_file():
        raise FlowIntegrationError(
            f"Reference image cho scene không tồn tại: {start_image}"
        )

    try:
        aspect = Aspect.from_cli(project.settings.aspect_ratio)
        model = _map_video_model(safe_model)
        request = GenerateVideoRequest(
            prompt=flow_prompt(scene)
            + (
                "\n\nRUNTIME QC REPAIR INSTRUCTION:\n"
                + scene.runtime_repair_instruction.strip()
                if scene.runtime_repair_instruction.strip()
                else ""
            ),
            mode=Mode.I2V if start_image is not None else Mode.T2V,
            aspect=aspect,
            model=model,
            duration=_request_duration(model, scene.duration),
            count=1,
            start_image=start_image,
        )
    except FlowIntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError(
            f"Cấu hình video không tương thích với gflow: {type(exc).__name__}: {exc}"
        ) from exc

    upstream_project_id = project.flow_project_id

    def on_started(started: Any) -> None:
        nonlocal upstream_project_id
        started_project = str(getattr(started, "project_id", None) or "")
        if started_project:
            upstream_project_id = started_project
            project.flow_project_id = started_project
        scene.upstream_project_id = str(upstream_project_id or "")
        media_id = str(getattr(started, "media_id", None) or "")
        scene.provider_job_id = media_id
        scene.upstream_media_id = media_id
        scene.upstream_workflow_id = str(
            getattr(started, "flow_operation_id", None) or ""
        )
        scene.upstream_resource_name = media_id
        if checkpoint:
            checkpoint(project, scene)

    try:
        async with FlowApiClient(
            profile_dir=profile_path,
            headless=False,
            out_dir=output,
        ) as client:
            if not upstream_project_id:
                info = await client.create_project(title=project.name)
                upstream_project_id = str(info.project_id)
                project.flow_project_id = upstream_project_id
                scene.upstream_project_id = upstream_project_id
                if checkpoint:
                    checkpoint(project, scene)

            result = await client.generate_video(
                req=request,
                project_id=upstream_project_id,
                out_dir=output,
                poll_timeout_s=float(integration.timeout),
                download=True,
                on_started=on_started,
            )
    except FlowIntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError(
            f"Google Flow/gflow: {type(exc).__name__}: {exc}"
        ) from exc

    status = getattr(result, "status", None)
    if status is None or not bool(getattr(status, "succeeded", False)):
        detail = (
            getattr(status, "error_message", None)
            or ", ".join(getattr(status, "failure_reasons", ()) or ())
            or getattr(status, "status", None)
            or "unknown failure"
        )
        raise FlowIntegrationError(f"Google Flow/gflow generation thất bại: {detail}")

    media_id = str(getattr(status, "media_id", None) or scene.provider_job_id or "")
    resolved_project = str(
        getattr(result, "project_id", None) or upstream_project_id or ""
    )
    operation_id = str(
        getattr(result, "flow_operation_id", None) or scene.upstream_workflow_id or ""
    )
    scene.provider_job_id = media_id
    scene.upstream_media_id = media_id
    scene.upstream_project_id = resolved_project
    scene.upstream_workflow_id = operation_id
    scene.upstream_resource_name = media_id
    if resolved_project:
        project.flow_project_id = resolved_project

    local_path = getattr(result, "local_path", None)
    if local_path is None:
        raise FlowIntegrationError("gflow hoàn tất nhưng không trả về đường dẫn MP4")
    video = Path(str(local_path)).resolve()
    _validate_downloaded_mp4(video)

    if checkpoint:
        checkpoint(project, scene)

    try:
        result_file = video.relative_to(integration.data_root).as_posix()
    except ValueError as exc:
        raise FlowIntegrationError(
            "MP4 của gflow nằm ngoài thư mục dữ liệu ứng dụng"
        ) from exc

    last_frame_file = await integration._extract_last_frame(
        project.id, scene.id, video
    )
    return RenderResult(
        job_id=media_id,
        result_url=f"/api/projects/{project.id}/scenes/{scene.id}/video",
        result_file=result_file,
        last_frame_file=last_frame_file,
        upstream_project_id=resolved_project,
    )


async def _verify_migrated_profile_ui(profile_path: Path) -> tuple[bool, str]:
    """Read-only fallback for accounts whose Flow auth moved off the Labs NextAuth endpoint."""
    try:
        from gflow_cli.api.client import FlowApiClient

        async with FlowApiClient(
            profile_dir=profile_path,
            headless=False,
        ) as client:
            context = getattr(client, "_context", None)
            if context is None:
                return False, "Không tạo được browser context để xác minh Flow migrated UI."
            pages = list(getattr(context, "pages", ()) or ())
            page = pages[0] if pages else await context.new_page()
            await page.goto(
                "https://flow.google.com/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            await page.wait_for_timeout(1200)

            current_url = str(getattr(page, "url", "") or "")
            if "accounts.google.com" in current_url:
                return False, "Flow migrated UI yêu cầu đăng nhập Google."

            account_button = page.locator("button[aria-label='Account details']").first
            new_project = page.get_by_text("New project", exact=True).first
            project_links = page.locator("a[href*='/project/']")
            authenticated = (
                await account_button.is_visible()
                or await new_project.is_visible()
                or await project_links.count() > 0
            )
            if authenticated:
                return True, "Flow migrated UI session verified."
            return False, "Không xác nhận được phiên đăng nhập trên flow.google.com."
    except Exception as exc:  # noqa: BLE001
        return False, f"Không thể xác minh Flow migrated UI: {type(exc).__name__}"


async def verify_gflow_profile() -> tuple[bool, str, Path | None]:
    """Verify the exact gflow profile used by generation, including migrated Flow."""
    try:
        profile_path = resolve_gflow_profile_dir()
    except FlowIntegrationError as exc:
        return False, str(exc), None

    upstream_detail = ""
    try:
        from gflow_cli.auth.verification import (
            FlowSessionOutcome,
            verify_flow_profile,
        )

        verdict = await verify_flow_profile(
            profile_path,
            source="flow-story-studio",
        )
        upstream_detail = verdict.detail
        if verdict.outcome is FlowSessionOutcome.AUTHENTICATED:
            return True, verdict.detail, profile_path
    except Exception as exc:  # noqa: BLE001
        upstream_detail = f"gflow auth probe: {type(exc).__name__}"

    migrated_ok, migrated_detail = await _verify_migrated_profile_ui(profile_path)
    if migrated_ok:
        return True, migrated_detail, profile_path
    return False, migrated_detail or upstream_detail, profile_path


def _map_image_model(value: str) -> Any:
    from gflow_cli.api.image import Model

    aliases = {
        "nano-banana-pro": "nano-pro",
        "nano_banana_pro": "nano-pro",
        "nano-banana-2": "nano2",
        "nano_banana_2": "nano2",
    }
    normalized = value.strip().casefold()
    return Model.from_cli(aliases.get(normalized, normalized))


async def generate_reference_image_with_gflow(
    integration: Any,
    project_id: str,
    reference_id: str,
    prompt: str,
) -> str:
    """Generate one canonical 1:1 reference image through gflow."""
    try:
        from gflow_cli.api.client import FlowApiClient
        from gflow_cli.api.image import Aspect, GenerateImageRequest
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError("Không thể nạp gflow image transport") from exc

    profile_path = resolve_gflow_profile_dir()
    target_dir = integration.data_root / "references" / project_id / "entities"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{reference_id}.png"
    model_name = os.getenv("FLOW_REFERENCE_IMAGE_MODEL", "nano-pro")

    try:
        request = GenerateImageRequest(
            prompt=prompt,
            aspect=Aspect.SQUARE,
            model=_map_image_model(model_name),
            count=1,
        )
        async with FlowApiClient(
            profile_dir=profile_path,
            headless=False,
            out_dir=target_dir,
        ) as client:
            images = await client.generate_image(
                project_id=project_id or None,
                req=request,
                count=1,
            )
            if not images:
                raise FlowIntegrationError(
                    "Google Flow/gflow không trả về reference image"
                )
            final_path = await client.download_image(images[0], target)
    except FlowIntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError(
            f"Google Flow/gflow reference image: {type(exc).__name__}: {exc}"
        ) from exc

    resolved = Path(str(final_path)).resolve()
    if not resolved.is_file():
        raise FlowIntegrationError("gflow không lưu được reference image xuống máy")
    try:
        return resolved.relative_to(integration.data_root).as_posix()
    except ValueError as exc:
        raise FlowIntegrationError(
            "Reference image của gflow nằm ngoài thư mục dữ liệu ứng dụng"
        ) from exc
