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
                if await editor.is_visible(timeout=2000):
                    await editor.fill(prompt, timeout=5000)
                    await ui.page.wait_for_timeout(300)
                    text = (await editor.inner_text(timeout=2000)).strip()
                    if prompt[:20] in text and prompt[-20:] in text:
                        return
                    await editor.click(timeout=1500)
                    await ui.page.keyboard.press("Control+A")
                    await ui.page.keyboard.insert_text(prompt)
                    await ui.page.wait_for_timeout(300)
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
            settings_label = ui.page.get_by_text("Agent settings", exact=True).first
            if await settings_label.is_visible(timeout=300):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow Agent settings label probe failed: %s", exc)
        try:
            button = ui.page.locator('button[aria-label="Settings"]').first
            if not await button.is_visible(timeout=700):
                return False
            await button.click(timeout=1500)
            await ui.page.wait_for_timeout(500)
            return await ui.page.get_by_text("Agent settings", exact=True).first.is_visible(
                timeout=1000
            )
        except Exception:  # noqa: BLE001
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
        await flow_ui._studio_agent_original_select_aspect(
            ui, aspect, media_type=active_media
        )

    agent_aware_select_aspect._studio_compat = True  # type: ignore[attr-defined]
    flow_ui.FlowUI.select_aspect = agent_aware_select_aspect

    async def agent_aware_select_duration(ui: object, duration: int) -> None:
        if await ui.page.locator('button[aria-label="Settings"]').first.is_visible(timeout=300):
            # Current Agent UI has no duration control. The screenplay duration is
            # embedded in the production prompt; post-render QC verifies the result.
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
        await flow_ui._studio_agent_original_select_output_count(
            ui, count, media_type=active_media
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

    async def ensure_video_settings(ui: object) -> None:
        video_tab = ui.page.locator('[role="tab"]:has-text("Video")').first
        image_tab = ui.page.locator('[role="tab"]:has-text("Image")').first
        tabs_open = False
        try:
            tabs_open = await video_tab.is_visible(timeout=300) and await image_tab.is_visible(
                timeout=300
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Flow media-tab visibility probe failed: %s", exc)
        if not tabs_open:
            trigger = ui.page.locator('button:has-text("Video ·")').first
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
        if not await video_tab.is_visible(timeout=1000):
            raise RuntimeError("Could not open the Flow video settings panel")
        if (await video_tab.get_attribute("data-state")) != "active":
            await video_tab.click(timeout=1500)
            await ui.page.wait_for_timeout(400)
        if (await video_tab.get_attribute("data-state")) != "active":
            raise RuntimeError("Could not select the Flow video tab")

    async def select_video_tab_option(ui: object, label: str, kind: str) -> None:
        await ensure_video_settings(ui)
        option = ui.page.locator(f'[role="tab"]:has-text("{label}")').first
        if not await option.is_visible(timeout=1000):
            raise RuntimeError(f"Could not find Flow {kind} option {label!r}")
        if (await option.get_attribute("data-state")) != "active":
            await option.click(timeout=1500)
            await ui.page.wait_for_timeout(400)
        if (await option.get_attribute("data-state")) != "active":
            raise RuntimeError(f"Could not select Flow {kind} option {label!r}")

    if not hasattr(flow_ui, "_studio_original_select_aspect"):
        flow_ui._studio_original_select_aspect = flow_ui.FlowUI.select_aspect

    if not getattr(flow_ui.FlowUI.select_aspect, "_studio_compat", False):

        async def select_aspect(ui: object, aspect: str) -> None:
            if self._active_media_type == "image":
                await ui._click_tool_toggle("image")
                await flow_ui._studio_original_select_aspect(ui, aspect)
                return
            await select_video_tab_option(ui, aspect, "aspect")

        select_aspect._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_aspect = select_aspect

    if not getattr(flow_ui.FlowUI.select_duration, "_studio_compat", False):

        async def select_duration(ui: object, duration: int) -> None:
            await select_video_tab_option(ui, f"{duration}s", "duration")

        select_duration._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_duration = select_duration

    if not hasattr(flow_ui, "_studio_original_select_output_count"):
        flow_ui._studio_original_select_output_count = flow_ui.FlowUI.select_output_count

    if not getattr(flow_ui.FlowUI.select_output_count, "_studio_compat", False):

        async def select_output_count(ui: object, count: int) -> None:
            if self._active_media_type == "image":
                await flow_ui._studio_original_select_output_count(ui, count)
                return
            await select_video_tab_option(ui, f"x{count}", "output count")

        select_output_count._studio_compat = True  # type: ignore[attr-defined]
        flow_ui.FlowUI.select_output_count = select_output_count

    if getattr(flow_ui.FlowUI.select_model, "_studio_compat", False):
        return

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
    flow_ui.FlowUI.select_model = select_video_model