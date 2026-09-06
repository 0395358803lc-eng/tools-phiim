from types import SimpleNamespace

import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.flow_integration import gflow_transport
from flow_story_studio.models import AnalyzeRequest


def _project():
    return analyze_story(
        AnalyzeRequest(
            name="migrated image helper",
            original_text="SCENE 1 — ROOM — DAY\nAn empty table in soft window light.",
        )
    )


def test_flow_image_media_id_accepts_signed_migrated_url() -> None:
    url = (
        "https://flow-content.google/image/"
        "92c92488-c530-4a64-8e44-43ad323cc8d4"
        "?Expires=1&Signature=redacted"
    )
    assert (
        gflow_transport._image_media_id(url)
        == "92c92488-c530-4a64-8e44-43ad323cc8d4"
    )
    assert gflow_transport._image_media_id("https://example.com/image/nope") == ""


@pytest.mark.asyncio
async def test_ensure_migrated_project_creates_and_persists_flow_id() -> None:
    project = _project()
    expected = "5fa6b591-8d1c-4a59-9106-2b930a4661f2"

    class FakeButton:
        async def wait_for(self, **_kwargs):
            return None

        async def click(self, **_kwargs):
            page.url = f"https://flow.google.com/project/{expected}"

    class FakeRole:
        @property
        def first(self):
            return FakeButton()

    class FakePage:
        url = "about:blank"

        async def goto(self, url, **_kwargs):
            self.url = url

        def get_by_role(self, *_args, **_kwargs):
            return FakeRole()

        async def wait_for_timeout(self, _ms):
            return None

    page = FakePage()
    result = await gflow_transport._ensure_migrated_project(page, project)

    assert result == expected
    assert project.flow_project_id == expected


@pytest.mark.asyncio
async def test_ensure_migrated_project_reuses_existing_id_without_navigation() -> None:
    project = _project()
    project.flow_project_id = "5fa6b591-8d1c-4a59-9106-2b930a4661f2"

    page = SimpleNamespace()
    result = await gflow_transport._ensure_migrated_project(page, project)

    assert result == project.flow_project_id
