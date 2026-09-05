from pathlib import Path

import pytest

from flow_story_studio.flow_integration import FlowCLIIntegration
from flow_story_studio.flow_ui_contract import DEFAULT_FLOW_VIDEO_MODEL


class FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def press(self, value: str) -> None:
        self.events.append(f"press:{value}")

    async def insert_text(self, value: str) -> None:
        self.events.append(f"insert:{value}")


class BasicLocator:
    def __init__(
        self,
        *,
        visible: bool = False,
        text: str = "",
        disabled: bool = False,
        attributes: dict[str, str] | None = None,
    ) -> None:
        self.visible = visible
        self.text = text
        self.disabled = disabled
        self.attributes = attributes or {}
        self.clicked = 0
        self.filled = ""

    @property
    def first(self):
        return self

    async def is_visible(self, timeout=None):
        return self.visible

    async def is_disabled(self):
        return self.disabled

    async def click(self, timeout=None):
        self.clicked += 1

    async def fill(self, value: str, timeout=None):
        self.filled = value
        self.text = value

    async def inner_text(self, timeout=None):
        return self.text

    async def get_attribute(self, name: str):
        return self.attributes.get(name)

    def locator(self, _selector: str):
        return self


class CollectionLocator:
    def __init__(self, items: list[BasicLocator]) -> None:
        self.items = items

    @property
    def first(self):
        return self.items[0] if self.items else BasicLocator()

    async def count(self):
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]

    async def all_inner_texts(self):
        return [item.text for item in self.items]


class FakePage:
    def __init__(self) -> None:
        self.keyboard = FakeKeyboard()
        self.locators: dict[str, BasicLocator] = {}
        self.text_locators: dict[str, BasicLocator] = {}
        self.waits: list[int] = []

    def locator(self, selector: str):
        return self.locators.setdefault(selector, BasicLocator())

    def get_by_text(self, text: str, exact=True):
        return self.text_locators.setdefault(text, BasicLocator())

    async def wait_for_timeout(self, value: int):
        self.waits.append(value)


def _patched_flow(tmp_path: Path):
    import flow_cli._flow_ui as flow_ui

    integration = FlowCLIIntegration(tmp_path)
    integration._apply_flow_ui_compatibility()
    return integration, flow_ui


@pytest.mark.asyncio
async def test_prompt_adapter_retains_complete_prompt(tmp_path: Path) -> None:
    _integration, flow_ui = _patched_flow(tmp_path)
    page = FakePage()
    editor = BasicLocator(visible=True)
    page.locators[flow_ui.PROMPT_EDITOR_SELECTOR] = editor
    ui = flow_ui.FlowUI(page)
    prompt = "A" * 30 + " middle " + "Z" * 30

    await ui.set_prompt(prompt)

    assert editor.filled == prompt
    assert editor.text == prompt


@pytest.mark.asyncio
async def test_agent_duration_is_not_faked_when_control_is_absent(tmp_path: Path) -> None:
    _integration, flow_ui = _patched_flow(tmp_path)
    page = FakePage()
    page.locators['button[aria-label="Settings"]'] = BasicLocator(visible=True)
    ui = flow_ui.FlowUI(page)

    await ui.select_duration(8)

    assert page.locators['button[aria-label="Settings"]'].clicked == 0


@pytest.mark.asyncio
async def test_agent_selected_model_probe_strips_material_symbol(tmp_path: Path) -> None:
    integration, flow_ui = _patched_flow(tmp_path)
    integration._active_media_type = "video"
    page = FakePage()
    selector = 'button[aria-label="Video generation default model"]'
    page.locators[selector] = BasicLocator(
        visible=True,
        text="arrow_drop_down Veo 3.1 Lite [Lower Priority]",
    )
    ui = flow_ui.FlowUI(page)

    selected = await ui.get_selected_model()

    assert selected == "Veo 3.1 Lite [Lower Priority]"


@pytest.mark.asyncio
async def test_generate_adapter_prefers_agent_start_button(tmp_path: Path) -> None:
    _integration, flow_ui = _patched_flow(tmp_path)
    page = FakePage()
    selector = 'button[aria-label="Start generation"]'
    start = BasicLocator(visible=True, disabled=False)
    page.locators[selector] = start
    ui = flow_ui.FlowUI(page)

    await ui.click_generate()

    assert start.clicked == 1


def test_zero_credit_verify_model_adapter_is_strict(tmp_path: Path) -> None:
    _integration, flow_ui = _patched_flow(tmp_path)
    ui = flow_ui.FlowUI(FakePage())

    assert ui.verify_model_selection(
        DEFAULT_FLOW_VIDEO_MODEL,
        "arrow_drop_down Veo 3.1 Lite [Lower Priority]",
    )
    assert not ui.verify_model_selection(
        DEFAULT_FLOW_VIDEO_MODEL,
        "Veo 3.1 Lite",
    )


@pytest.mark.asyncio
async def test_agent_fallback_uses_radix_duration_adapter(tmp_path: Path) -> None:
    _integration, flow_ui = _patched_flow(tmp_path)
    page = FakePage()
    page.locators['button[aria-label="Settings"]'] = BasicLocator(visible=False)
    page.locators['[role="tab"]:has-text("Video")'] = BasicLocator(
        visible=True, attributes={"data-state": "active"}
    )
    page.locators['[role="tab"]:has-text("Image")'] = BasicLocator(
        visible=True, attributes={"data-state": "inactive"}
    )
    page.locators['[role="tab"]:has-text("8s")'] = BasicLocator(
        visible=True, attributes={"data-state": "active"}
    )
    ui = flow_ui.FlowUI(page)

    assert (
        flow_ui._studio_agent_original_select_duration
        is flow_ui._studio_radix_select_duration
    )
    await ui.select_duration(8)


@pytest.mark.asyncio
async def test_agent_fallback_zero_credit_model_uses_radix_selector(
    tmp_path: Path
) -> None:
    _integration, flow_ui = _patched_flow(tmp_path)
    page = FakePage()
    page.locators['button[aria-label="Settings"]'] = BasicLocator(visible=False)
    page.locators['[role="tab"]:has-text("Video")'] = BasicLocator(
        visible=True, attributes={"data-state": "active"}
    )
    page.locators['[role="tab"]:has-text("Image")'] = BasicLocator(
        visible=True, attributes={"data-state": "inactive"}
    )
    menu_trigger = BasicLocator(visible=True, text="Omni 1.1 Flash")
    page.locators['button:visible[aria-haspopup="menu"]'] = CollectionLocator(
        [menu_trigger]
    )
    wrong = BasicLocator(visible=True, text="Veo 3.1 Lite")
    correct = BasicLocator(
        visible=True, text="Veo 3.1 Lite [Lower Priority]"
    )
    options_selector = (
        '[role="menuitem"]:visible, [role="menuitemradio"]:visible, '
        '[role="option"]:visible, [role="radio"]:visible, button:visible'
    )
    page.locators[options_selector] = CollectionLocator([wrong, correct])
    ui = flow_ui.FlowUI(page)

    async def selected_model():
        return "Veo 3.1 Lite [Lower Priority]"

    ui.get_selected_model = selected_model

    assert (
        flow_ui._studio_agent_original_select_model
        is flow_ui._studio_radix_select_model
    )
    await ui.select_model(DEFAULT_FLOW_VIDEO_MODEL)

    assert menu_trigger.clicked == 1
    assert correct.clicked == 1
    assert wrong.clicked == 0


@pytest.mark.asyncio
async def test_agent_fallback_uses_radix_aspect_adapter(tmp_path: Path) -> None:
    _integration, flow_ui = _patched_flow(tmp_path)
    page = FakePage()
    page.locators['button[aria-label="Settings"]'] = BasicLocator(visible=False)
    page.locators['[role="tab"]:has-text("Video")'] = BasicLocator(
        visible=True, attributes={"data-state": "active"}
    )
    page.locators['[role="tab"]:has-text("Image")'] = BasicLocator(
        visible=True, attributes={"data-state": "inactive"}
    )
    aspect = BasicLocator(visible=True, attributes={"data-state": "active"})
    page.locators['[role="tab"]:has-text("16:9")'] = aspect
    ui = flow_ui.FlowUI(page)

    assert flow_ui._studio_agent_original_select_aspect is flow_ui._studio_radix_select_aspect
    await ui.select_aspect("16:9", media_type="video")
    assert aspect.visible is True
