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
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "legacy")
    project = analyze_story(AnalyzeRequest(name="flow lifecycle", original_text=SCRIPT))
    project.settings.provider = "google-flow"
    scene = project.scenes[0]
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(flow.vault, "load", lambda: ({"SID": "valid"}, None))
    monkeypatch.setattr(
        generation_module, "can_attach_existing_chrome", lambda _p: False
    )

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
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "legacy")
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

    project = analyze_story(AnalyzeRequest(name="legacy reference", original_text=SCRIPT))
    relative = await flow.generate_reference_image(
        project,
        "CHAR_001",
        "canonical reference",
    )

    assert relative == f"references/{project.id}/entities/CHAR_001.png"
    assert (tmp_path / relative).read_bytes() == b"reference"


@pytest.mark.asyncio
async def test_reference_image_generation_fails_closed_without_downloadable_image(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "legacy")
    flow = FlowCLIIntegration(tmp_path)
    flow._save_cookies({"SID": "valid"}, None)
    monkeypatch.setattr(generation_module, "can_attach_existing_chrome", lambda _p: False)

    class FakeClient:
        async def generate_image(self, **_kwargs):
            return []

    monkeypatch.setattr(flow, "_client", lambda _cookies: FakeClient())

    project = analyze_story(AnalyzeRequest(name="legacy reference fail", original_text=SCRIPT))
    with pytest.raises(Exception, match="reference image"):
        await flow.generate_reference_image(project, "CHAR_001", "reference")


@pytest.mark.asyncio
async def test_generate_uses_live_project_and_browser_polling_when_cdp_ready(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "legacy")
    project = analyze_story(AnalyzeRequest(name="live flow", original_text=SCRIPT))
    project.settings.provider = "google-flow"
    scene = project.scenes[0]
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(flow.vault, "load", lambda: ({}, None))
    monkeypatch.setattr(
        generation_module, "can_attach_existing_chrome", lambda _p: True
    )

    ensured: list[str] = []

    async def fake_ensure(_port_file):
        ensured.append("project")
        return "live-project"

    monkeypatch.setattr(generation_module, "ensure_live_flow_project", fake_ensure)

    client_project_ids: list[str | None] = []

    class FakeClient:
        async def create_project(self, *_args, **_kwargs):
            raise AssertionError("legacy create_project must not run in CDP mode")

        async def wait_for_video(self, *_args, **_kwargs):
            raise AssertionError("legacy wait_for_video must not run in CDP mode")

    client = FakeClient()

    def fake_client(_cookies, project_id=None):
        client_project_ids.append(project_id)
        return client

    monkeypatch.setattr(flow, "_client", fake_client)

    job = SimpleNamespace(
        job_id="live-job",
        is_success=False,
        workflow_id="workflow-live",
        media_id="media-live",
        resource_name="resource-live",
        status="RUNNING",
    )

    async def fake_generate_video(_self, _client, **_kwargs):
        return job

    monkeypatch.setattr(generation_module, "_generate_video", fake_generate_video)

    async def fake_wait(_self, project_id, submitted, output, **kwargs):
        assert project_id == "live-project"
        assert submitted is job
        assert kwargs["timeout"] == flow.timeout
        output.mkdir(parents=True, exist_ok=True)
        video = output / "live.mp4"
        video.write_bytes(b"0000ftyp" + b"x" * 2048)
        return [video]

    monkeypatch.setattr(generation_module, "wait_for_browser_video", fake_wait)
    monkeypatch.setattr(
        flow,
        "_extract_last_frame",
        lambda *_args, **_kwargs: _async_value("frames/live-last.jpg"),
    )

    result = await flow.generate(project, scene)

    assert ensured == ["project"]
    assert client_project_ids == ["live-project"]
    assert project.flow_project_id == "live-project"
    assert scene.upstream_project_id == "live-project"
    assert scene.provider_job_id == "live-job"
    assert job.status == "SUCCEEDED"
    assert result.result_file.endswith("/live.mp4")
