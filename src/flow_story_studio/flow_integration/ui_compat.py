"""Flow UI compatibility shims applied to the vendored Flow CLI.

Relocated verbatim from the former monolith so the monkey-patching stays
isolated from browser, session and generation concerns. The single parameter
is deliberately named ``self`` so the historical body stays byte-identical.
"""

from __future__ import annotations

import logging

from ..flow_ui_contract import (
    DEFAULT_FLOW_VIDEO_MODEL,
    FlowUIContractError,
    choose_model_candidate,
    model_aliases,
    model_matches_contract,
)

logger = logging.getLogger(__name__)


def apply_flow_ui_compatibility(self) -> None:
    """Teach Flow CLI 0.6.0 how the current Radix tab controls expose selection."""
    import flow_cli._flow_ui as flow_ui

    flow_ui.VIDEO_MODEL_UI_LABELS[DEFAULT_FLOW_VIDEO_MODEL] = list(
        model_aliases(DEFAULT_FLOW_VIDEO_MODEL)
    )

    # Live Flow (Sep 2026): ProseMirror prompt box + Angular Material
    # settings/model controls. Keep legacy selectors as fallbacks.
    flow_ui.PROMPT_EDITOR_SELECTOR = (
        'div.ProseMirror[contenteditable="true"]'
    )
    flow_ui.VIDEO_SETTINGS_SELECTOR = (
        'button[aria-label="Settings trigger"], button:has-text("Video")'
    )
    flow_ui.MODEL_READ_SELECTORS = (
        'button[aria-label="Select model family"]',
        *tuple(flow_ui.MODEL_READ_SELECTORS),
    )

    if not getattr(flow_ui.FlowUI.goto_flow, "_studio_live_flow", False):
        async def goto_flow(ui: object, timeout: int = 30000) -> None:
            await ui.page.goto(
                "https://flow.google.com/",
                wait_until="domcontentloaded",
                timeout=timeout,
            )
            await ui.page.wait_for_timeout(1200)
            for selector in flow_ui.DISMISS_SELECTORS:
                try:
                    button = ui.page.locator(selector).first
                    if await button.is_visible(timeout=500):
                        await button.click(timeout=1000)
                        await ui.page.wait_for_timeout(200)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Flow live dismiss selector failed: %s", exc)

        goto_flow._studio_live_flow = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.goto_flow = goto_flow

    if not getattr(flow_ui.FlowUI.ensure_project, "_studio_live_flow", False):
        async def ensure_project(ui: object, project_id: str | None = None) -> str:
            if project_id:
                await ui.page.goto(
                    f"https://flow.google.com/project/{project_id}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await ui.page.wait_for_timeout(1200)
                if f"/project/{project_id}" not in ui.page.url:
                    raise RuntimeError(
                        "Flow did not navigate to the requested live project: "
                        f"{ui.page.url}"
                    )
                return project_id
            url = ui.page.url
            if "/project/" in url:
                return (
                    url.split("/project/", 1)[1]
                    .split("/", 1)[0]
                    .split("?", 1)[0]
                    .split("#", 1)[0]
                )
            return ""

        ensure_project._studio_live_flow = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.ensure_project = ensure_project

    current = tuple(flow_ui.SELECTED_OPTION_TEMPLATES)
    additions = (
        '[role="tab"][aria-selected="true"]:has-text("{label}")',
        '[role="tab"][data-state="active"]:has-text("{label}")',
    )
    flow_ui.SELECTED_OPTION_TEMPLATES = additions + tuple(
        item for item in current if item not in additions
    )
    if not getattr(flow_ui.FlowUI.verify_model_selection, "_studio_compat", False):
        original_verify_model = flow_ui.FlowUI.verify_model_selection

        def verify_model_selection(ui: object, requested: str, selected: str) -> bool:
            # Material Symbols are rendered as text inside the current
            # model trigger and are not part of the selected model's name.
            cleaned = selected.replace("arrow_drop_down", "").strip()
            if requested.strip().casefold().replace(" ", "-") == DEFAULT_FLOW_VIDEO_MODEL:
                return model_matches_contract(requested, cleaned)
            return original_verify_model(ui, requested, cleaned)

        verify_model_selection._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.verify_model_selection = verify_model_selection

    if not getattr(flow_ui.FlowUI.set_prompt, "_studio_compat", False):
        original_set_prompt = flow_ui.FlowUI.set_prompt

        async def set_prompt(ui: object, prompt: str) -> None:
            editor = ui.page.locator(flow_ui.PROMPT_EDITOR_SELECTOR).first
            try:
                # Flow mounts the ProseMirror editor shortly after project navigation.
                # Playwright's is_visible() is an immediate probe here, so poll explicitly.
                ready = False
                for _ in range(20):
                    try:
                        if await editor.count() and await editor.is_visible():
                            ready = True
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Flow prompt-editor readiness probe failed: %s", exc)
                    await ui.page.wait_for_timeout(250)
                if ready:
                    await editor.click(timeout=1500)
                    await ui.page.keyboard.press("Control+A")
                    await ui.page.keyboard.press("Backspace")
                    await ui.page.keyboard.insert_text(prompt)
                    await ui.page.wait_for_timeout(400)
                    text = (await editor.inner_text(timeout=2000)).strip()
                    if prompt[:20] in text and prompt[-20:] in text:
                        return
                    # Fallback for older contenteditable implementations.
                    await editor.fill(prompt, timeout=5000)
                    await ui.page.wait_for_timeout(400)
                    text = (await editor.inner_text(timeout=2000)).strip()
                    if prompt[:20] in text and prompt[-20:] in text:
                        return
                    raise RuntimeError("Flow prompt editor did not retain the complete prompt")
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow prompt-editor compatibility path failed: %s", exc)
            await original_set_prompt(ui, prompt)

        set_prompt._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.set_prompt = set_prompt

    async def agent_settings_open(ui: object) -> bool:
        try:
            live_video = ui.page.locator('[role="radio"]:has-text("Video")').first
            live_image = ui.page.locator('[role="radio"]:has-text("Image")').first
            if await live_video.is_visible(timeout=300) and await live_image.is_visible(
                timeout=300
            ):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow live settings visibility probe failed: %s", exc)
        try:
            settings_label = ui.page.get_by_text("Agent settings", exact=True).first
            if await settings_label.is_visible(timeout=300):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow Agent settings label probe failed: %s", exc)
        for selector in (
            'button[aria-label="Settings trigger"]',
            'button[aria-label="Settings"]',
        ):
            try:
                button = ui.page.locator(selector).first
                if not await button.is_visible(timeout=500):
                    continue
                await button.click(timeout=1500)
                await ui.page.wait_for_timeout(400)
                live_video = ui.page.locator('[role="radio"]:has-text("Video")').first
                if await live_video.is_visible(timeout=700):
                    return True
                if await ui.page.get_by_text(
                    "Agent settings", exact=True
                ).first.is_visible(timeout=700):
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow settings trigger %s failed: %s", selector, exc)
        return False

    async def agent_section(ui: object, label: str) -> object:
        if not await agent_settings_open(ui):
            return None
        section_label = ui.page.get_by_text(label, exact=True).first
        if not await section_label.is_visible(timeout=800):
            return None
        return section_label.locator("xpath=parent::div[contains(@class,'settings-section')]")

    async def agent_select_toggle(ui: object, label: str, value: str) -> bool:
        section = await agent_section(ui, label)
        if section is None:
            return False
        option = section.locator(f'button[role="radio"]:has-text("{value}")').first
        if not await option.is_visible(timeout=800):
            return False
        if (await option.get_attribute("aria-checked")) != "true":
            await option.click(timeout=1500)
            await ui.page.wait_for_timeout(300)
        if (await option.get_attribute("aria-checked")) != "true":
            raise RuntimeError(f"Flow Agent settings did not select {label}={value}")
        return True

    async def agent_select_model(ui: object, media_type: str, model: str) -> bool:
        label = (
            "Image generation default"
            if media_type == "image"
            else "Video generation default"
        )
        section = await agent_section(ui, label)
        if section is None:
            return False
        aria = f"{label} model"
        picker = section.locator(f'button[aria-label="{aria}"]').first
        if not await picker.is_visible(timeout=800):
            return False
        normalized = model.strip().lower().replace(" ", "-")
        mapping = (
            flow_ui.IMAGE_MODEL_UI_LABELS
            if media_type == "image"
            else flow_ui.VIDEO_MODEL_UI_LABELS
        )
        candidates = list(mapping.get(normalized, [model]))
        current = (await picker.inner_text(timeout=500)).replace("arrow_drop_down", "").strip()
        if model_matches_contract(model, current):
            return True
        await picker.click(timeout=1500)
        await ui.page.wait_for_timeout(300)
        if normalized == DEFAULT_FLOW_VIDEO_MODEL:
            options = ui.page.locator(
                '[role="menuitem"]:visible, [role="menuitemradio"]:visible, '
                '[role="option"]:visible, [role="radio"]:visible, button:visible'
            )
            labels = await options.all_inner_texts()
            try:
                selected = choose_model_candidate(model, labels)
            except FlowUIContractError as exc:
                await ui.page.keyboard.press("Escape")
                raise RuntimeError(str(exc)) from exc
            option = options.nth(selected.index)
            await option.click(timeout=1500)
            await ui.page.wait_for_timeout(300)
            updated = (await picker.inner_text(timeout=500)).replace(
                "arrow_drop_down", ""
            ).strip()
            if not model_matches_contract(model, updated):
                raise RuntimeError(
                    f"Requested zero-credit model {model!r}, but Flow selected {updated!r}"
                )
            return True
        for candidate in candidates:
            option = ui.page.locator(f'[role="menuitem"]:has-text("{candidate}")').first
            try:
                if await option.is_visible(timeout=700):
                    await option.click(timeout=1500)
                    await ui.page.wait_for_timeout(300)
                    updated = (
                        await picker.inner_text(timeout=500)
                    ).replace("arrow_drop_down", "").strip()
                    if not flow_ui.model_matches(model, updated):
                        raise RuntimeError(
                            f"Requested model {model!r}, but Flow Agent selected {updated!r}"
                        )
                    return True
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow Agent model candidate %r failed: %s", candidate, exc)
        await ui.page.keyboard.press("Escape")
        raise RuntimeError(f"Could not select Flow Agent model {model!r}")

    async def agent_disable_confirmation_and_save(ui: object) -> bool:
        section = await agent_section(ui, "Confirm before generating")
        if section is None:
            return False
        never = section.get_by_text("Never", exact=True).first
        try:
            radio = section.locator('input[type="radio"][value="2"]').first
            if await radio.is_visible(timeout=500):
                if not await radio.is_checked():
                    await radio.click(timeout=1500)
            elif await never.is_visible(timeout=500):
                await never.click(timeout=1500)
            else:
                raise RuntimeError("Flow Agent confirmation setting 'Never' is unavailable")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Could not disable Flow Agent confirmation prompt") from exc
        save = ui.page.get_by_role("button", name="Save", exact=True).first
        if not await save.is_visible(timeout=800):
            raise RuntimeError("Flow Agent settings Save button is unavailable")
        await save.click(timeout=1500)
        await ui.page.wait_for_timeout(500)
        return True

    if not hasattr(flow_ui, "_studio_agent_original_select_aspect"):
        flow_ui._studio_agent_original_select_aspect = flow_ui.FlowUI.select_aspect
    if not hasattr(flow_ui, "_studio_agent_original_select_duration"):
        flow_ui._studio_agent_original_select_duration = flow_ui.FlowUI.select_duration
    if not hasattr(flow_ui, "_studio_agent_original_select_output_count"):
        flow_ui._studio_agent_original_select_output_count = flow_ui.FlowUI.select_output_count
    if not hasattr(flow_ui, "_studio_agent_original_select_model"):
        flow_ui._studio_agent_original_select_model = flow_ui.FlowUI.select_model
    if not hasattr(flow_ui, "_studio_agent_original_select_image_model"):
        flow_ui._studio_agent_original_select_image_model = flow_ui.FlowUI.select_image_model
    if not hasattr(flow_ui, "_studio_agent_original_get_selected_model"):
        flow_ui._studio_agent_original_get_selected_model = flow_ui.FlowUI.get_selected_model

    # Preserve the vendored methods separately. Agent-aware wrappers below use
    # compatibility fallbacks that may be refreshed on each call with the
    # current integration instance, while raw methods must never become recursive.
    if not hasattr(flow_ui, "_studio_raw_select_aspect"):
        flow_ui._studio_raw_select_aspect = flow_ui._studio_agent_original_select_aspect
    if not hasattr(flow_ui, "_studio_raw_select_duration"):
        flow_ui._studio_raw_select_duration = flow_ui._studio_agent_original_select_duration
    if not hasattr(flow_ui, "_studio_raw_select_output_count"):
        flow_ui._studio_raw_select_output_count = flow_ui._studio_agent_original_select_output_count
    if not hasattr(flow_ui, "_studio_raw_select_model"):
        flow_ui._studio_raw_select_model = flow_ui._studio_agent_original_select_model
    if not hasattr(flow_ui, "_studio_raw_select_image_model"):
        flow_ui._studio_raw_select_image_model = flow_ui._studio_agent_original_select_image_model
    if not hasattr(flow_ui, "_studio_raw_get_selected_model"):
        flow_ui._studio_raw_get_selected_model = flow_ui._studio_agent_original_get_selected_model

    async def call_media_compatible(
        function: object,
        ui: object,
        value: object,
        media_type: str,
    ) -> None:
        try:
            await function(ui, value, media_type=media_type)
        except TypeError as exc:
            if "media_type" not in str(exc):
                raise
            await function(ui, value)

    async def agent_aware_select_aspect(
        ui: object, aspect: str, media_type: str | None = None
    ) -> None:
        active_media = media_type or self._active_media_type
        label = (
            "Image generation default"
            if active_media == "image"
            else "Video generation default"
        )
        if await agent_select_toggle(ui, label, aspect):
            return
        await call_media_compatible(
            flow_ui._studio_agent_original_select_aspect,
            ui,
            aspect,
            active_media,
        )

    agent_aware_select_aspect._studio_compat = True  # type: ignore[attr-defined]
    flow_ui.FlowUI.select_aspect = agent_aware_select_aspect

    async def agent_aware_select_duration(ui: object, duration: int) -> None:
        live_settings = ui.page.locator(
            'button[aria-label="Settings trigger"]'
        ).first
        if await live_settings.is_visible(timeout=300):
            await flow_ui._studio_agent_original_select_duration(ui, duration)
            return
        if await ui.page.locator(
            'button[aria-label="Settings"]'
        ).first.is_visible(timeout=300):
            # Legacy Agent UI did not expose a duration control.
            return
        await flow_ui._studio_agent_original_select_duration(ui, duration)

    agent_aware_select_duration._studio_compat = True  # type: ignore[attr-defined]
    flow_ui.FlowUI.select_duration = agent_aware_select_duration

    async def agent_aware_select_output_count(
        ui: object, count: int, media_type: str | None = None
    ) -> None:
        active_media = media_type or self._active_media_type
        label = (
            "Image generation default"
            if active_media == "image"
            else "Video generation default"
        )
        if await agent_select_toggle(ui, label, f"x{count}"):
            await agent_disable_confirmation_and_save(ui)
            return
        await call_media_compatible(
            flow_ui._studio_agent_original_select_output_count,
            ui,
            count,
            active_media,
        )

    agent_aware_select_output_count._studio_compat = True  # type: ignore[attr-defined]
    flow_ui.FlowUI.select_output_count = agent_aware_select_output_count

    async def agent_aware_select_video_model(ui: object, model: str) -> None:
        if await agent_select_model(ui, "video", model):
            return
        await flow_ui._studio_agent_original_select_model(ui, model)

    agent_aware_select_video_model._studio_compat = True  # type: ignore[attr-defined]
    flow_ui.FlowUI.select_model = agent_aware_select_video_model

    async def agent_aware_select_image_model(ui: object, model: str) -> None:
        if await agent_select_model(ui, "image", model):
            return
        await flow_ui._studio_agent_original_select_image_model(ui, model)

    agent_aware_select_image_model._studio_compat = True  # type: ignore[attr-defined]
    flow_ui.FlowUI.select_image_model = agent_aware_select_image_model

    async def agent_aware_get_selected_model(ui: object) -> str:
        live_picker = ui.page.locator(
            'button[aria-label="Select model family"]'
        ).first
        try:
            if await live_picker.is_visible(timeout=500):
                return (
                    (await live_picker.inner_text())
                    .replace("arrow_drop_down", "")
                    .strip()
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow live selected-model probe failed: %s", exc)
        label = (
            "Image generation default model"
            if self._active_media_type == "image"
            else "Video generation default model"
        )
        try:
            picker = ui.page.locator(f'button[aria-label="{label}"]').first
            if await picker.is_visible(timeout=500):
                return (await picker.inner_text()).replace("arrow_drop_down", "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow Agent selected-model probe failed: %s", exc)
        return await flow_ui._studio_agent_original_get_selected_model(ui)

    flow_ui.FlowUI.get_selected_model = agent_aware_get_selected_model

    if not getattr(flow_ui.FlowUI.click_generate, "_studio_agent_compat", False):
        original_click_generate = flow_ui.FlowUI.click_generate

        async def click_generate(ui: object) -> None:
            warning = ui.page.locator(
                'button[aria-label="Insufficient credits warning"]'
            ).first
            try:
                if await warning.is_visible(timeout=300):
                    raise RuntimeError(
                        "Flow reports insufficient credits; refusing generation."
                    )
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow credit-warning probe failed: %s", exc)
            start = ui.page.locator('button[aria-label="Start generation"]').first
            try:
                if await start.is_visible(timeout=700) and not await start.is_disabled():
                    await start.click(timeout=5000)
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow Agent start-generation button fallback: %s", exc)
            await original_click_generate(ui)

        click_generate._studio_agent_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.click_generate = click_generate

    async def control_selected(control: object) -> bool:
        return (
            (await control.get_attribute("aria-checked")) == "true"
            or (await control.get_attribute("aria-selected")) == "true"
            or (await control.get_attribute("data-state")) == "active"
        )

    async def visible_media_control(ui: object, label: str) -> object:
        live = ui.page.locator(f'[role="radio"]:has-text("{label}")').first
        try:
            if await live.is_visible(timeout=300):
                return live
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow live media control %s probe failed: %s", label, exc)
        return ui.page.locator(f'[role="tab"]:has-text("{label}")').first

    async def ensure_video_settings(ui: object) -> None:
        video_option = await visible_media_control(ui, "Video")
        image_option = await visible_media_control(ui, "Image")
        options_open = False
        try:
            options_open = await video_option.is_visible(
                timeout=300
            ) and await image_option.is_visible(timeout=300)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow media-option visibility probe failed: %s", exc)
        if not options_open:
            trigger = ui.page.locator(
                'button[aria-label="Settings trigger"]'
            ).first
            if not await trigger.is_visible(timeout=500):
                trigger = ui.page.locator('button:has-text("Video")').first
            if not await trigger.is_visible(timeout=500):
                menu_buttons = ui.page.locator('button:visible[aria-haspopup="menu"]')
                trigger = None
                for index in range((await menu_buttons.count()) - 1, -1, -1):
                    candidate = menu_buttons.nth(index)
                    try:
                        label = (await candidate.inner_text(timeout=500)).strip().lower()
                        if any(name in label for name in ("omni", "veo", "nano", "imagen")):
                            trigger = candidate
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Flow media-settings trigger candidate failed: %s", exc)
                        continue
                if trigger is None:
                    raise RuntimeError("Could not find the Flow media settings button")
            await trigger.click(timeout=1500)
            await ui.page.wait_for_timeout(400)
            video_option = await visible_media_control(ui, "Video")
        if not await video_option.is_visible(timeout=1000):
            raise RuntimeError("Could not open the Flow video settings panel")
        if not await control_selected(video_option):
            await video_option.click(timeout=1500)
            await ui.page.wait_for_timeout(400)
        if not await control_selected(video_option):
            raise RuntimeError("Could not select the Flow video option")

    async def select_video_tab_option(ui: object, label: str, kind: str) -> None:
        await ensure_video_settings(ui)
        option = await visible_media_control(ui, label)
        if not await option.is_visible(timeout=1000):
            raise RuntimeError(f"Could not find Flow {kind} option {label!r}")
        if not await control_selected(option):
            await option.click(timeout=1500)
            await ui.page.wait_for_timeout(400)
        if not await control_selected(option):
            raise RuntimeError(f"Could not select Flow {kind} option {label!r}")

    if not hasattr(flow_ui, "_studio_original_select_aspect"):
        flow_ui._studio_original_select_aspect = flow_ui._studio_agent_original_select_aspect
    if not hasattr(flow_ui, "_studio_radix_select_aspect"):

        async def radix_select_aspect(
            ui: object, aspect: str, media_type: str | None = None
        ) -> None:
            active_media = media_type or self._active_media_type
            if active_media == "image":
                await ui._click_tool_toggle("image")
                await flow_ui._studio_original_select_aspect(ui, aspect)
                return
            await select_video_tab_option(ui, aspect, "aspect")

        radix_select_aspect._studio_compat = True  # type: ignore[attr-defined]
        flow_ui._studio_radix_select_aspect = radix_select_aspect
    flow_ui._studio_agent_original_select_aspect = flow_ui._studio_radix_select_aspect

    if not hasattr(flow_ui, "_studio_radix_select_duration"):

        async def radix_select_duration(ui: object, duration: int) -> None:
            await select_video_tab_option(ui, f"{duration}s", "duration")

        radix_select_duration._studio_compat = True  # type: ignore[attr-defined]
        flow_ui._studio_radix_select_duration = radix_select_duration
    flow_ui._studio_agent_original_select_duration = flow_ui._studio_radix_select_duration

    if not hasattr(flow_ui, "_studio_original_select_output_count"):
        flow_ui._studio_original_select_output_count = (
            flow_ui._studio_agent_original_select_output_count
        )
    if not hasattr(flow_ui, "_studio_radix_select_output_count"):

        async def radix_select_output_count(
            ui: object, count: int, media_type: str | None = None
        ) -> None:
            active_media = media_type or self._active_media_type
            if active_media == "image":
                await flow_ui._studio_original_select_output_count(ui, count)
                return
            await select_video_tab_option(ui, f"x{count}", "output count")

        radix_select_output_count._studio_compat = True  # type: ignore[attr-defined]
        flow_ui._studio_radix_select_output_count = radix_select_output_count
    flow_ui._studio_agent_original_select_output_count = (
        flow_ui._studio_radix_select_output_count
    )

    async def select_video_model(ui: object, model: str) -> None:
        await ensure_video_settings(ui)
        normalized = model.strip().lower().replace(" ", "-")
        candidates = list(flow_ui.VIDEO_MODEL_UI_LABELS.get(normalized, [model]))

        opened_nested_menu = False
        # The current Flow UI can default video generation to Omni, so the
        # nested model trigger is not guaranteed to contain the word "Veo".
        # It is the innermost visible menu button whose label names a model.
        buttons = ui.page.locator('button:visible[aria-haspopup="menu"]')
        for index in range((await buttons.count()) - 1, -1, -1):
            button = buttons.nth(index)
            try:
                label = (await button.inner_text(timeout=500)).strip().lower()
                if any(name in label for name in ("omni", "veo", "nano", "imagen")):
                    await button.click(timeout=1500)
                    await ui.page.wait_for_timeout(400)
                    opened_nested_menu = True
                    break
            except Exception as exc:  # noqa: BLE001
                logger.debug("Flow nested model trigger candidate failed: %s", exc)
                continue

        if normalized == DEFAULT_FLOW_VIDEO_MODEL:
            options = ui.page.locator(
                '[role="menuitem"]:visible, [role="menuitemradio"]:visible, '
                '[role="option"]:visible, [role="radio"]:visible, button:visible'
            )
            labels = await options.all_inner_texts()
            try:
                selected = choose_model_candidate(model, labels)
            except FlowUIContractError as exc:
                await ui.page.keyboard.press("Escape")
                raise RuntimeError(str(exc)) from exc
            await options.nth(selected.index).click(timeout=1500)
            await ui.page.wait_for_timeout(400)
            selected_label = await ui.get_selected_model()
            if not model_matches_contract(model, selected_label):
                raise RuntimeError(
                    "Flow model selector changed after click; refusing generation."
                )
            return

        templates = tuple(
            dict.fromkeys(flow_ui.IMAGE_MODEL_OPTION_TEMPLATES + flow_ui.MODEL_OPTION_TEMPLATES)
        )
        for candidate in candidates:
            for template in templates:
                try:
                    option = ui.page.locator(template.format(label=candidate)).first
                    if await option.is_visible(timeout=1000):
                        await option.click(timeout=1500)
                        await ui.page.wait_for_timeout(400)
                        return
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Flow model option candidate %r failed: %s", candidate, exc
                    )
                    continue
        diagnostics = self.data_root / "diagnostics" / "flow-model-menu.png"
        diagnostics.parent.mkdir(parents=True, exist_ok=True)
        try:
            await ui.page.screenshot(path=str(diagnostics))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow diagnostics screenshot failed: %s", exc)
        try:
            labels = [
                text.strip()
                for text in await ui.page.locator("button:visible").all_inner_texts()
                if text.strip()
            ][:30]
        except Exception:  # noqa: BLE001
            labels = []
        await ui.page.keyboard.press("Escape")
        detail = " after opening nested model menu" if opened_nested_menu else ""
        raise RuntimeError(f"Could not find model {model!r}{detail}; visible controls={labels}")

    select_video_model._studio_compat = True  # type: ignore[attr-defined]
    flow_ui._studio_radix_select_model = select_video_model
    flow_ui._studio_agent_original_select_model = flow_ui._studio_radix_select_model