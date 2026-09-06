"""Modern gflow-cli transport for the current flow.google.com UI."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..flow_helpers import flow_prompt, reference_path
from ..models import Project, Scene
from ..providers.base import RenderResult
from .errors import FlowIntegrationError, RenderCheckpoint

logger = logging.getLogger(__name__)


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


async def _locator_carries_media_id(locator: Any, media_id: str) -> bool:
    """Whether a migrated Flow DOM node exposes the requested media UUID."""
    needle = media_id.strip().casefold()
    if not needle:
        return False
    try:
        html = str(await locator.evaluate("(e) => e.outerHTML"))
        if needle in html.casefold():
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Media ID DOM probe failed, trying image src: %s", exc)
    try:
        images = locator.locator("img")
        for index in range(await images.count()):
            image = images.nth(index)
            source = str(await image.get_attribute("src") or "")
            try:
                current = str(await image.evaluate("(e) => e.currentSrc || ''"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Image currentSrc probe failed: %s", exc)
                current = ""
            if needle in source.casefold() or needle in current.casefold():
                return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Media ID image locator probe failed: %s", exc)
    return False


async def _wait_bound_media_id(
    page: Any,
    bound: Any,
    media_id: str,
    *,
    timeout_ms: int,
) -> bool:
    """Wait until the Start chip is visible and carries the exact uploaded media id."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if (
                await bound.count()
                and await bound.is_visible()
                and await _locator_carries_media_id(bound, media_id)
            ):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Media ID binding poll probe failed: %s", exc)
        await page.wait_for_timeout(100)
    return False


def _apply_migrated_ui_compat() -> None:
    """Patch narrow upstream drift observed on the migrated Flow composer."""
    try:
        from gflow_cli.api.transports import migrated_composer
        from gflow_cli.errors import UiSelectorDriftError
    except Exception as exc:  # noqa: BLE001
        logger.debug("Migrated UI compat patches skipped (gflow not available): %s", exc)
        return

    # Flow can render two responsive copies of the settings trigger at once:
    # the first carries ``hidden`` while the second is visible. gflow <= 0.68.0
    # resolves ``.first`` and waits forever on the hidden copy. Restricting the
    # selector to the visible DOM branch preserves the upstream driver/wire logic.
    if getattr(migrated_composer, "READY_ANCHOR", "") == ".settings-trigger-button":
        migrated_composer.READY_ANCHOR = ".settings-trigger-button:not([hidden])"

    # The application stages every I2V frame under a per-attempt unique filename,
    # removing duplicate-name ambiguity. The narrow picker/upload compatibility
    # below adds a second fail-closed invariant: the bound Start chip must carry
    # the exact media UUID returned by the upload RPC before submit is allowed.
    composer_cls = getattr(migrated_composer, "MigratedComposer", None)
    marker = "_flow_story_sticky_picker_compat"
    if composer_cls is None or getattr(composer_cls, marker, False):
        return

    bound_selector = getattr(
        migrated_composer,
        "BOUND_CHIP",
        "flow-prompt-box button.chip-container:has(img)",
    )
    picker_search = getattr(migrated_composer, "PICKER_SEARCH", "input[type='text']")
    picker_option = getattr(
        migrated_composer,
        "PICKER_OPTION",
        "button.asset-item[role='option']",
    )
    search_attempts = int(getattr(migrated_composer, "FRAME_SEARCH_ATTEMPTS", 3))
    search_timeout_s = float(getattr(migrated_composer, "FRAME_PICKER_OPEN_S", 8.0))
    retry_pause_s = float(
        getattr(migrated_composer, "FRAME_SEARCH_RETRY_PAUSE_S", 2.0)
    )

    async def _pick_frame_with_sticky_overlay_compat(
        self: Any,
        page: Any,
        name: str,
        media_id: str,
    ) -> None:
        """Bind one uniquely named uploaded frame on the migrated picker.

        The current Flow picker is server-filtered and can expose a selected detail
        row before the search debounce has settled. Waiting briefly, requiring one
        exact filename match, and then requiring a real bound-chip thumbnail keeps
        the operation fail-closed instead of silently binding an older duplicate.
        """
        bound = page.locator(bound_selector).first
        exact_name = re.compile(r"^\s*" + re.escape(name) + r"\s*$")

        for attempt in range(1, search_attempts + 1):
            picker = await self._open_frame_picker(page)
            search = picker.locator(picker_search).first
            await search.wait_for(state="visible", timeout=8_000)
            await search.fill(name)
            # Search is server-side. The live migrated UI needs a short debounce
            # before the virtualized result list represents the typed filename.
            await page.wait_for_timeout(1_000)

            options = picker.locator(picker_option).filter(has_text=exact_name)
            try:
                await options.first.wait_for(
                    state="visible",
                    timeout=int(search_timeout_s * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                listed = [
                    item.strip()
                    for item in await picker.locator(picker_option).all_text_contents()
                ]
                await page.keyboard.press("Escape")
                if attempt < search_attempts:
                    await page.wait_for_timeout(int(retry_pause_s * 1000))
                    continue
                raise UiSelectorDriftError(
                    detail=(
                        f"migrated host: no exact frame asset named {name!r} after "
                        f"{search_attempts} searches (uploaded media {media_id}); "
                        f"picker listed: {', '.join(repr(x) for x in listed[:8]) or 'nothing'}"
                    )
                ) from exc

            count = await options.count()
            if count < 1:
                raise UiSelectorDriftError(
                    detail=(
                        f"migrated host: frame picker returned no exact match for {name!r} "
                        f"after upload of media {media_id}"
                    )
                )

            target = options.first
            if count > 1:
                matches: list[Any] = []
                for index in range(count):
                    candidate = options.nth(index)
                    if await _locator_carries_media_id(candidate, media_id):
                        matches.append(candidate)
                if len(matches) != 1:
                    raise UiSelectorDriftError(
                        detail=(
                            f"migrated host: frame picker returned {count} exact filename "
                            f"matches for {name!r}, but {len(matches)} carried uploaded media "
                            f"{media_id}; refusing an ambiguous I2V bind"
                        )
                    )
                target = matches[0]

            await target.click(timeout=4_000)

            # Some cohorts commit immediately; others reveal an explicit detail
            # action. A visible chip is insufficient: it must carry the exact media
            # UUID returned by the upload RPC, otherwise an older duplicate asset
            # can be submitted and billed.
            if await _wait_bound_media_id(
                page,
                bound,
                media_id,
                timeout_ms=2_500,
            ):
                return

            confirm = picker.locator("button.detail-add-to-prompt-btn").first
            if await confirm.count() and await confirm.is_visible():
                await confirm.click(timeout=5_000)

            if not await _wait_bound_media_id(
                page,
                bound,
                media_id,
                timeout_ms=8_000,
            ):
                raise UiSelectorDriftError(
                    detail=(
                        f"migrated host: frame {name!r} was selected but the Start chip "
                        f"did not bind uploaded media {media_id}"
                    )
                )
            return

        raise UiSelectorDriftError(
            detail=f"migrated host: unable to bind frame {name!r} for media {media_id}"
        )

    composer_cls._pick_frame_by_name = _pick_frame_with_sticky_overlay_compat
    setattr(composer_cls, marker, True)

    upload_marker = "_flow_story_upload_mouse_compat"
    if not getattr(composer_cls, upload_marker, False):
        composer_cls._upload_via_toolbar = _upload_via_toolbar_mouse_compat
        setattr(composer_cls, upload_marker, True)

    prompt_marker = "_flow_story_clean_prompt_compat"
    if not getattr(composer_cls, prompt_marker, False):
        original_send_prompt = composer_cls.send_prompt
        composer_selector = getattr(
            migrated_composer,
            "COMPOSER",
            "[contenteditable='true']",
        )

        async def _send_prompt_without_stale_draft(
            self: Any,
            page: Any,
            prompt: str,
        ) -> None:
            # Flow can preserve an unsent draft in a reused project after an
            # aborted/failed run. Upstream insert_text() appends to that draft,
            # which can leave Start generation disabled even though the new prompt
            # itself is valid. Clear only the text editor; the bound Start chip is
            # outside this contenteditable and remains intact.
            editor = page.locator(composer_selector).first
            if await editor.count():
                await editor.fill("")
                await page.wait_for_timeout(50)
            await original_send_prompt(self, page, prompt)

        composer_cls.send_prompt = _send_prompt_without_stale_draft
        setattr(composer_cls, prompt_marker, True)


async def _upload_via_toolbar_mouse_compat(
    self: Any,
    page: Any,
    project_id: str,
    image_path: Path,
) -> str:
    """Upload a local frame using a stable mouse gesture on migrated Flow.

    Flow's current ``xapfileselectortrigger`` detaches the Upload menu item as the
    native file chooser opens. Playwright ``Locator.click`` can therefore time out
    after the user gesture has already begun. A short settle followed by a real
    mouse click on the element's bounding box consistently emits ``filechooser``.
    """
    from gflow_cli.api.transports import migrated_composer
    from gflow_cli.errors import MediaUploadRejectedError, UiSelectorDriftError

    loop = asyncio.get_running_loop()
    reply: asyncio.Future[tuple[int, str]] = loop.create_future()
    route = f"batchexecute:{migrated_composer.UPLOAD_RPC}"

    async def on_response(response: Any) -> None:
        url = str(getattr(response, "url", ""))
        rpcid = (
            migrated_composer._rpcid(url)
            if "batchexecute" in url
            else None
        )
        if (
            rpcid != migrated_composer.UPLOAD_RPC
            or reply.done()
        ):
            return
        status = int(getattr(response, "status", 0) or 0)
        body = ""
        if status == 200:
            try:
                body = await response.text()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to read HTTP 200 response body: %s", exc)
                body = ""
        if not reply.done():
            reply.set_result((status, body))

    page.on("response", on_response)
    try:
        chooser = None
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                if attempt > 1:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                add = page.locator(migrated_composer.TOOLBAR_ADD).first
                if not await add.count():
                    raise UiSelectorDriftError(
                        detail=(
                            "migrated host: toolbar add button missing before frame upload "
                            "(host=migrated)"
                        )
                    )
                await add.click(timeout=5_000)
                item = page.locator(migrated_composer.UPLOAD_MENU_ITEM).first
                await item.wait_for(
                    state="visible",
                    timeout=int(migrated_composer.FRAME_PICKER_OPEN_S * 1000),
                )
                await page.wait_for_timeout(500)
                box = await item.bounding_box()
                if not box:
                    raise UiSelectorDriftError(
                        detail=(
                            "migrated host: Upload menu item has no clickable box "
                            "(host=migrated)"
                        )
                    )
                async with page.expect_file_chooser(
                    timeout=int(migrated_composer.FRAME_PICKER_OPEN_S * 1000)
                ) as fc_info:
                    await page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                chooser = await fc_info.value
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 3:
                    raise UiSelectorDriftError(
                        detail=(
                            "migrated host: Upload did not emit a file chooser after "
                            "3 stabilized mouse-click attempts (host=migrated)"
                        )
                    ) from exc
        if chooser is None:
            raise UiSelectorDriftError(
                detail=(
                    "migrated host: frame upload ended without a file chooser "
                    "(host=migrated; last_error="
                    f"{type(last_error).__name__ if last_error else 'none'})"
                )
            )
        await chooser.set_files(str(image_path))
        try:
            status, body = await asyncio.wait_for(
                reply,
                timeout=migrated_composer.FRAME_UPLOAD_S,
            )
        except TimeoutError:
            raise MediaUploadRejectedError(
                detail=(
                    f"migrated host: no {migrated_composer.UPLOAD_RPC} reply within "
                    f"{migrated_composer.FRAME_UPLOAD_S:.0f}s of choosing the file"
                ),
                route=route,
            ) from None
        if status != 200:
            raise MediaUploadRejectedError(
                detail=(
                    f"migrated host: upload rpc {migrated_composer.UPLOAD_RPC} "
                    f"answered HTTP {status}"
                ),
                status=status,
                route=route,
            )
        media_id = migrated_composer._first_uuid(body)
        if media_id is None:
            raise MediaUploadRejectedError(
                detail=(
                    f"migrated host: {migrated_composer.UPLOAD_RPC} answered 200 "
                    "without a media id"
                ),
                status=status,
                route=route,
            )
        if media_id.lower() == project_id.lower():
            raise MediaUploadRejectedError(
                detail=(
                    "migrated host: upload response returned the project id where a "
                    "new media id was expected"
                ),
                status=status,
                route=route,
            )
        return media_id
    finally:
        page.remove_listener("response", on_response)


_FLOW_PROJECT_RE = re.compile(
    r"/project/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)",
    re.IGNORECASE,
)
_FLOW_IMAGE_RE = re.compile(
    r"/image/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\?|$)",
    re.IGNORECASE,
)


def _project_id_from_url(url: str) -> str:
    match = _FLOW_PROJECT_RE.search(url or "")
    return match.group(1) if match else ""


def _image_media_id(url: str) -> str:
    match = _FLOW_IMAGE_RE.search(url or "")
    return match.group(1) if match else ""


def _image_model_label(value: str) -> str:
    aliases = {
        "nano-pro": "Nano Banana Pro",
        "nano-banana-pro": "Nano Banana Pro",
        "nano_banana_pro": "Nano Banana Pro",
        "nano2": "Nano Banana 2",
        "nano-banana-2": "Nano Banana 2",
        "nano_banana_2": "Nano Banana 2",
        "nano2-lite": "Nano Banana 2 Lite",
        "nano-banana-2-lite": "Nano Banana 2 Lite",
        "nano_banana_2_lite": "Nano Banana 2 Lite",
    }
    normalized = value.strip().casefold()
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise FlowIntegrationError(f"Model reference image không hỗ trợ: {value}") from exc


async def _ensure_migrated_project(page: Any, project: Project) -> str:
    """Create a Flow project through the migrated UI when the app has no upstream id."""
    if project.flow_project_id:
        return project.flow_project_id

    await page.goto("https://flow.google.com/", wait_until="domcontentloaded", timeout=45_000)
    button = page.get_by_role("button", name="New project", exact=True).first
    try:
        await button.wait_for(state="visible", timeout=10_000)
        await button.click(timeout=5_000)
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError(
            "Không thể tạo Flow project mới trên flow.google.com"
        ) from exc

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        project_id = _project_id_from_url(str(page.url))
        if project_id:
            project.flow_project_id = project_id
            return project_id
        await page.wait_for_timeout(250)
    raise FlowIntegrationError("Flow không điều hướng tới project mới sau khi bấm New project")


async def _open_migrated_settings(page: Any) -> Any:
    pane = page.locator(".cdk-overlay-pane:visible").filter(
        has=page.locator("[role='radiogroup']")
    ).last
    try:
        if await pane.count() and await pane.is_visible():
            return pane
    except Exception as exc:  # noqa: BLE001
        logger.debug("Migrated settings pane visibility probe failed: %s", exc)

    trigger = page.locator(".settings-trigger-button:not([hidden])").first
    await trigger.wait_for(state="visible", timeout=10_000)
    await trigger.click(timeout=5_000)
    pane = page.locator(".cdk-overlay-pane:visible").filter(
        has=page.locator("[role='radiogroup']")
    ).last
    await pane.locator("[role='radio']").first.wait_for(state="visible", timeout=8_000)
    return pane


async def _select_radio(pane: Any, name: str) -> None:
    radio = pane.get_by_role("radio", name=name, exact=True).first
    await radio.wait_for(state="visible", timeout=8_000)
    if (await radio.get_attribute("aria-checked")) != "true":
        await radio.click(timeout=5_000)


async def _drive_migrated_reference_image(
    client: Any,
    project: Project,
    prompt: str,
    *,
    model_name: str,
    timeout_s: float,
) -> tuple[str, str]:
    """Drive migrated Flow image UI and return (signed image URL, media id)."""
    page = client.page
    upstream_project_id = await _ensure_migrated_project(page, project)
    target = f"https://flow.google.com/project/{upstream_project_id}"
    if not str(page.url).startswith(target):
        await page.goto(target, wait_until="domcontentloaded", timeout=45_000)

    pane = await _open_migrated_settings(page)
    await _select_radio(pane, "Image")

    model_button = page.locator("button[aria-label='Select model family']").first
    await model_button.wait_for(state="visible", timeout=8_000)
    await model_button.click(timeout=5_000)
    menu = page.locator(".cdk-overlay-pane:visible").filter(
        has=page.locator("[role='menuitem']")
    ).last
    label = _image_model_label(model_name)
    item = menu.get_by_role("menuitem").filter(has_text=re.compile(re.escape(label), re.I)).first
    await item.wait_for(state="visible", timeout=8_000)
    await item.click(timeout=5_000)

    # Model changes can reset the aspect, so bind aspect/count afterwards.
    pane = await _open_migrated_settings(page)
    await _select_radio(pane, "1:1")
    await _select_radio(pane, "x1")
    await page.keyboard.press("Escape")

    sources = await page.locator("img[src*='flow-content.google/image/']").evaluate_all(
        "els => els.map(e => e.currentSrc || e.src).filter(Boolean)"
    )
    baseline = {_image_media_id(str(src)) for src in sources if _image_media_id(str(src))}

    editor = page.locator("div.ProseMirror[contenteditable='true']").first
    await editor.wait_for(state="visible", timeout=10_000)
    await editor.fill(prompt)

    credit_warning = page.locator("button[aria-label='Insufficient credits warning']").first
    try:
        if await credit_warning.count() and await credit_warning.is_visible():
            raise FlowIntegrationError("Google Flow báo không đủ credit cho reference image")
    except FlowIntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("Credit warning visibility probe failed: %s", exc)

    generate = page.locator("button[aria-label='Start generation']").first
    await generate.wait_for(state="visible", timeout=10_000)
    deadline = time.monotonic() + 5.0
    while await generate.is_disabled() and time.monotonic() < deadline:
        await page.wait_for_timeout(100)
    if await generate.is_disabled():
        raise FlowIntegrationError("Nút tạo reference image vẫn bị vô hiệu sau khi nhập prompt")
    await generate.click(timeout=5_000)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        candidates = await page.locator(
            "img[src*='flow-content.google/image/']"
        ).evaluate_all(
            "els => els.filter(e => e.complete && e.naturalWidth > 0)"
            ".map(e => e.currentSrc || e.src)"
        )
        for raw in candidates:
            source = str(raw)
            media_id = _image_media_id(source)
            if media_id and media_id not in baseline:
                parts = urlsplit(source)
                if parts.scheme == "https" and parts.hostname == "flow-content.google":
                    return source, media_id
        await page.wait_for_timeout(1000)
    raise FlowIntegrationError("Hết thời gian chờ reference image mới từ Flow")


def _correct_reference_extension(path: Path) -> Path:
    head = path.read_bytes()[:16]
    suffix = ""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif head.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        suffix = ".webp"
    if not suffix:
        raise FlowIntegrationError("Reference image tải về không có định dạng ảnh hợp lệ")
    if path.suffix.casefold() == suffix:
        return path
    target = path.with_suffix(suffix)
    if target.exists():
        target.unlink()
    path.replace(target)
    return target


def _flow_api_client(*, profile_dir: Path, headless: bool, out_dir: Path | None = None) -> Any:
    """Build a gflow client with the Windows migrated-cookie compatibility seam.

    gflow's pre-read cookie fallback may launch a second persistent Chrome against
    the same profile when browser-cookie3 cannot decrypt the Windows cookie DB.
    The fallback exists for macOS basic-store seeding; on the migrated Windows
    Flow path the headed context authenticates through the Google session itself.
    Skipping only this best-effort pre-read avoids a second-profile race while
    preserving upstream behaviour everywhere else.
    """
    from gflow_cli.api.client import FlowApiClient

    kwargs: dict[str, Any] = {
        "profile_dir": profile_dir,
        "headless": headless,
    }
    if out_dir is not None:
        kwargs["out_dir"] = out_dir

    if sys.platform != "win32":
        return FlowApiClient(**kwargs)

    class _WindowsMigratedFlowApiClient(FlowApiClient):
        async def _preread_flow_session_cookies(self) -> None:
            self._preread_flow_cookies = {}

    return _WindowsMigratedFlowApiClient(**kwargs)


def _stage_unique_i2v_frame(
    data_root: Path,
    project_id: str,
    scene_id: str,
    source: Path,
) -> Path:
    """Copy an I2V source frame to a per-attempt unique upload filename."""
    staging_dir = data_root / ".gflow-staging" / project_id / scene_id
    staging_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or ".png"
    staged = staging_dir / f"{scene_id}-{uuid4().hex}{suffix}"
    shutil.copy2(source, staged)
    return staged


def _cleanup_staged_i2v_frame(path: Path | None, data_root: Path) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
        parent = path.parent
        stop = (data_root / ".gflow-staging").resolve()
        while parent.exists() and parent.resolve() != stop:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        try:
            stop.rmdir()
        except OSError:
            pass
    except OSError:
        pass


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

    staged_start_image: Path | None = None
    try:
        aspect = Aspect.from_cli(project.settings.aspect_ratio)
        model = _map_video_model(safe_model)
        request_start_image = start_image
        if start_image is not None:
            staged_start_image = _stage_unique_i2v_frame(
                integration.data_root,
                project.id,
                scene.id,
                start_image,
            )
            request_start_image = staged_start_image
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
            start_image=request_start_image,
        )
    except FlowIntegrationError:
        _cleanup_staged_i2v_frame(staged_start_image, integration.data_root)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup_staged_i2v_frame(staged_start_image, integration.data_root)
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
        async with _flow_api_client(
            profile_dir=profile_path,
            headless=False,
            out_dir=output,
        ) as client:
            if not upstream_project_id:
                upstream_project_id = await _ensure_migrated_project(client.page, project)
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
    finally:
        _cleanup_staged_i2v_frame(staged_start_image, integration.data_root)

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
        async with _flow_api_client(
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


async def generate_reference_image_with_gflow(
    integration: Any,
    project: Project,
    reference_id: str,
    prompt: str,
) -> str:
    """Generate one canonical 1:1 reference image on migrated flow.google.com."""
    if not gflow_available():
        raise FlowIntegrationError("Không thể nạp gflow image transport")

    _apply_migrated_ui_compat()
    profile_path = resolve_gflow_profile_dir()
    target_dir = integration.data_root / "references" / project.id / "entities"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{reference_id}.png"
    model_name = os.getenv("FLOW_REFERENCE_IMAGE_MODEL", "nano-pro")

    try:
        async with _flow_api_client(
            profile_dir=profile_path,
            headless=False,
            out_dir=target_dir,
        ) as client:
            source, _media_id = await _drive_migrated_reference_image(
                client,
                project,
                prompt,
                model_name=model_name,
                timeout_s=min(float(integration.timeout), 300.0),
            )
            downloaded = await client.download(source, target)
            final_path = _correct_reference_extension(Path(str(downloaded)).resolve())
    except FlowIntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FlowIntegrationError(
            f"Google Flow/gflow reference image: {type(exc).__name__}: {exc}"
        ) from exc

    if not final_path.is_file():
        raise FlowIntegrationError("gflow không lưu được reference image xuống máy")
    try:
        return final_path.relative_to(integration.data_root).as_posix()
    except ValueError as exc:
        raise FlowIntegrationError(
            "Reference image của gflow nằm ngoài thư mục dữ liệu ứng dụng"
        ) from exc
