from pathlib import Path
from types import SimpleNamespace

import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.flow_integration import FlowCLIIntegration
from flow_story_studio.flow_integration import generation as generation_module
from flow_story_studio.models import AnalyzeRequest

SCRIPT = """
SCENE 1 — ROOM — NIGHT
A person crosses the room and stops beside the table.
"""


@pytest.mark.asyncio
async def test_generate_video_retries_headed_after_headless_403(
    monkeypatch, tmp_path: Path
) -> None:
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(generation_module, "can_attach_existing_chrome", lambda _p: False)
    monkeypatch.setenv("FLOW_BROWSER_HEADLESS", "1")
    calls: list[bool] = []

    class FakeClient:
        async def generate_video(self, **kwargs):
            calls.append(kwargs["headless"])
            if kwargs["headless"]:
                raise RuntimeError("upstream HTTP 403")
            return "headed-ok"

    result = await generation_module._generate_video(
        flow,
        FakeClient(),
        prompt="x",
        aspect="16:9",
        model="veo-3.1-lite-lower-priority",
        duration=8,
        image_path=None,
        timeout=30,
    )

    assert result == "headed-ok"
    assert calls == [True, False]
    assert flow._force_headed_browser is True


@pytest.mark.asyncio
async def test_generate_video_uses_existing_chrome_when_available(
    monkeypatch, tmp_path: Path
) -> None:
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(generation_module, "can_attach_existing_chrome", lambda _p: True)
    captured: dict[str, object] = {}

    async def fake_existing(self, client, **kwargs):
        captured.update(kwargs)
        captured["client"] = client
        return "chrome-ok"

    monkeypatch.setattr(generation_module, "_generate_with_existing_chrome", fake_existing)

    class FakeClient:
        async def _generate_via_browser(self, **_kwargs):
            return None

    client = FakeClient()
    result = await generation_module._generate_video(
        flow,
        client,
        prompt="x",
        aspect="16:9",
        model="veo-3.1-lite-lower-priority",
        timeout=30,
    )

    assert result == "chrome-ok"
    assert captured["media_type"] == "video"
    assert captured["count"] == 1
    assert captured["client"] is client


@pytest.mark.asyncio
async def test_generate_with_existing_chrome_restores_media_type(
    monkeypatch, tmp_path: Path
) -> None:
    flow = FlowCLIIntegration(tmp_path)
    flow._active_media_type = "video"
    monkeypatch.setattr(generation_module, "can_attach_existing_chrome", lambda _p: True)

    manager_token = object()

    class FakeManager:
        def __init__(self, _path):
            pass

        async def __aenter__(self):
            return manager_token

        async def __aexit__(self, _exc_type, _exc, _tb):
            return None

    monkeypatch.setattr(generation_module, "_ExistingChromeManager", FakeManager)

    class FakeClient:
        async def _generate_via_browser(self, **kwargs):
            assert flow._active_media_type == "image"
            assert kwargs["manager"] is manager_token
            assert kwargs["headless"] is False
            return "image-ok"

    result = await generation_module._generate_with_existing_chrome(
        flow,
        FakeClient(),
        media_type="image",
        prompt="reference",
        aspect="1:1",
        model="nano-banana-pro",
        timeout=30,
    )

    assert result == "image-ok"
    assert flow._active_media_type == "video"


@pytest.mark.asyncio
async def test_generate_persists_job_identity_and_checkpoints(
    monkeypatch, tmp_path: Path
) -> None:
    project = analyze_story(AnalyzeRequest(name="flow lifecycle", original_text=SCRIPT))
    project.settings.provider = "google-flow"
    scene = project.scenes[0]
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(flow.vault, "load", lambda: ({"SID": "valid"}, None))

    class FakeClient:
        async def create_project(self, name, media_type):
            assert name == project.name
            assert media_type == "video"
            return "up-project"

    client = FakeClient()
    monkeypatch.setattr(flow, "_client", lambda _cookies, _project_id=None: client)

    job = SimpleNamespace(
        job_id="job-1",
        is_success=True,
        workflow_id="workflow-1",
        media_id="media-1",
        resource_name="resource-1",
    )

    async def fake_generate_video(_self, _client, **_kwargs):
        return job

    monkeypatch.setattr(generation_module, "_generate_video", fake_generate_video)

    async def fake_download(_client, _completed, output, project_id):
        assert project_id == "up-project"
        output.mkdir(parents=True, exist_ok=True)
        video = output / "flow.mp4"
        video.write_bytes(b"0000ftyp" + b"x" * 2048)
        return [video]

    monkeypatch.setattr(flow, "_download_completed", fake_download)
    monkeypatch.setattr(
        flow,
        "_extract_last_frame",
        lambda *_args, **_kwargs: _async_value("frames/last.jpg"),
    )
    checkpoints: list[str] = []

    result = await flow.generate(
        project,
        scene,
        checkpoint=lambda _project, current: checkpoints.append(current.provider_job_id),
    )

    assert project.flow_project_id == "up-project"
    assert scene.provider_job_id == "job-1"
    assert scene.upstream_project_id == "up-project"
    assert scene.upstream_workflow_id == "workflow-1"
    assert scene.upstream_media_id == "media-1"
    assert scene.upstream_resource_name == "resource-1"
    assert result.job_id == "job-1"
    assert result.upstream_project_id == "up-project"
    assert result.result_file.endswith("/flow.mp4")
    assert len(checkpoints) >= 3


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_reference_image_generation_downloads_canonical_asset(
    monkeypatch, tmp_path: Path
) -> None:
    from flow_cli import _downloader

    flow = FlowCLIIntegration(tmp_path)
    flow._save_cookies({"SID": "valid"}, None)
    monkeypatch.setattr(generation_module, "can_attach_existing_chrome", lambda _p: False)

    class FakeClient:
        async def generate_image(self, **kwargs):
            assert kwargs["aspect"] == "1:1"
            assert kwargs["count"] == 1
            return [SimpleNamespace(fife_url="https://example.invalid/reference.png")]

    monkeypatch.setattr(flow, "_client", lambda _cookies: FakeClient())

    def fake_download(_url, target, *, cookies, kind):
        assert cookies == {"SID": "valid"}
        assert kind == "image"
        Path(target).write_bytes(b"reference")

    monkeypatch.setattr(_downloader, "download_file", fake_download)

    relative = await flow.generate_reference_image(
        "project-1",
        "CHAR_001",
        "canonical reference",
    )

    assert relative == "references/project-1/entities/CHAR_001.png"
    assert (tmp_path / relative).read_bytes() == b"reference"


@pytest.mark.asyncio
async def test_reference_image_generation_fails_closed_without_downloadable_image(
    monkeypatch, tmp_path: Path
) -> None:
    flow = FlowCLIIntegration(tmp_path)
    flow._save_cookies({"SID": "valid"}, None)
    monkeypatch.setattr(generation_module, "can_attach_existing_chrome", lambda _p: False)

    class FakeClient:
        async def generate_image(self, **_kwargs):
            return []

    monkeypatch.setattr(flow, "_client", lambda _cookies: FakeClient())

    with pytest.raises(Exception, match="reference image"):
        await flow.generate_reference_image("project-1", "CHAR_001", "reference")
