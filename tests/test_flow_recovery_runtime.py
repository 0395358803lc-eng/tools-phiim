from types import SimpleNamespace

import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.flow_integration import FlowCLIIntegration, FlowIntegrationError
from flow_story_studio.flow_integration import recovery as recovery_module
from flow_story_studio.models import AnalyzeRequest

SCRIPT = """
SCENE 1 — ROOM — NIGHT
A person crosses the room and stops beside the table.
"""


@pytest.mark.asyncio
async def test_download_completed_uses_cli_mp4_without_browser(tmp_path, monkeypatch) -> None:
    flow = FlowCLIIntegration(tmp_path)
    mp4 = tmp_path / "downloaded.mp4"
    mp4.write_bytes(b"valid-enough-test-file")

    class Client:
        async def download(self, _raw, dest_dir):
            assert dest_dir == tmp_path / "out"
            return [mp4]

    async def forbidden_browser(*_args, **_kwargs):
        raise AssertionError("browser fallback should not run")

    monkeypatch.setattr(flow, "_download_via_browser", forbidden_browser)
    completed = SimpleNamespace(raw={"ok": True})

    files = await flow._download_completed(
        Client(),
        completed,
        tmp_path / "out",
        "project-upstream",
    )

    assert files == [mp4]


@pytest.mark.asyncio
async def test_download_completed_falls_back_to_browser(tmp_path, monkeypatch) -> None:
    flow = FlowCLIIntegration(tmp_path)
    recovered = tmp_path / "out" / "recovered.mp4"
    recovered.parent.mkdir(parents=True)
    recovered.write_bytes(b"browser-recovered")

    class Client:
        async def download(self, _raw, dest_dir):
            raise RuntimeError("403")

    async def browser_fallback(project_id, completed, output):
        assert project_id == "project-upstream"
        assert completed.raw == {"job": 1}
        assert output == tmp_path / "out"
        return [recovered]

    monkeypatch.setattr(flow, "_download_via_browser", browser_fallback)

    files = await flow._download_completed(
        Client(),
        SimpleNamespace(raw={"job": 1}),
        tmp_path / "out",
        "project-upstream",
    )

    assert files == [recovered]


@pytest.mark.asyncio
async def test_browser_recovery_selects_exact_matching_video(tmp_path, monkeypatch) -> None:
    from flow_cli import _browser

    flow = FlowCLIIntegration(tmp_path)
    flow._chrome_port_file = tmp_path / "missing-DevToolsActivePort"
    flow._save_cookies({"SID": "test"}, None)
    body = b"\x00\x00\x00\x18ftypisom" + (b"x" * 2048)

    class FakeResponse:
        status = 200
        headers = {"content-type": "video/mp4"}

        async def body(self):
            return body

    class FakeRequest:
        async def get(self, url, timeout):
            assert "name=media-123" in url
            assert timeout == 180_000
            return FakeResponse()

    class FakeLocator:
        async def evaluate_all(self, _script):
            return [
                {
                    "src": "/download?name=other",
                    "href": "/edit/other",
                    "tile_id": "other",
                    "media_key": "other",
                },
                {
                    "src": "/download?name=media-123",
                    "href": "/edit/workflow-123",
                    "tile_id": "workflow-123",
                    "media_key": "media-123",
                },
            ]

    class FakePage:
        def __init__(self):
            self.request = FakeRequest()

        async def goto(self, url, wait_until, timeout):
            assert "project/project-upstream" in url
            assert wait_until == "domcontentloaded"
            assert timeout == 30_000

        async def wait_for_timeout(self, _ms):
            return None

        def locator(self, selector):
            assert selector == "video[src]"
            return FakeLocator()

        async def reload(self, **_kwargs):
            return None

    class FakeBrowserManager:
        def __init__(self, **_kwargs):
            self.page = FakePage()

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return None

    monkeypatch.setattr(_browser, "BrowserManager", FakeBrowserManager)
    job = SimpleNamespace(
        job_id="job-123",
        workflow_id="workflow-123",
        media_id="media-123",
        resource_name=None,
        operation_name=None,
        raw={},
    )

    files = await flow._download_via_browser(
        "project-upstream",
        job,
        tmp_path / "renders",
    )

    assert len(files) == 1
    assert files[0].name == "flow_media-123.mp4"
    assert files[0].read_bytes() == body


@pytest.mark.asyncio
async def test_recover_submitted_returns_original_job_identity(tmp_path, monkeypatch) -> None:
    project = analyze_story(AnalyzeRequest(name="recover success", original_text=SCRIPT))
    scene = project.scenes[0]
    scene.provider_job_id = "job-existing"
    scene.upstream_project_id = "project-existing"
    scene.upstream_workflow_id = "workflow-existing"

    flow = FlowCLIIntegration(tmp_path)

    async def recover(_project_id, _job, output):
        output.mkdir(parents=True, exist_ok=True)
        video = output / "recovered.mp4"
        video.write_bytes(b"recovered-video")
        return [video]

    async def frame(_project_id, _scene_id, _video):
        return "references/project/scene-last.jpg"

    monkeypatch.setattr(flow, "_download_via_browser", recover)
    monkeypatch.setattr(flow, "_extract_last_frame", frame)

    result = await flow._recover_submitted(project, scene)

    assert result is not None
    assert result.job_id == "job-existing"
    assert result.upstream_project_id == "project-existing"
    assert result.result_file.endswith("/recovered.mp4")
    assert result.last_frame_file.endswith("scene-last.jpg")


@pytest.mark.asyncio
async def test_recover_submitted_rejects_non_mp4_recovery(tmp_path, monkeypatch) -> None:
    project = analyze_story(AnalyzeRequest(name="recover no mp4", original_text=SCRIPT))
    scene = project.scenes[0]
    scene.provider_job_id = "job-existing"
    scene.upstream_project_id = "project-existing"
    flow = FlowCLIIntegration(tmp_path)

    async def recover(_project_id, _job, output):
        output.mkdir(parents=True, exist_ok=True)
        image = output / "preview.png"
        image.write_bytes(b"preview")
        return [image]

    monkeypatch.setattr(flow, "_download_via_browser", recover)

    with pytest.raises(FlowIntegrationError, match="no recoverable MP4"):
        await flow._recover_submitted(project, scene)


@pytest.mark.asyncio
async def test_browser_recovery_prefers_cdp_session(tmp_path, monkeypatch) -> None:
    flow = FlowCLIIntegration(tmp_path)
    flow._chrome_port_file = tmp_path / "DevToolsActivePort"
    body = b"\x00\x00\x00\x18ftypisom" + (b"x" * 2048)
    page_closed: list[bool] = []

    class FakeResponse:
        status = 200
        headers = {"content-type": "video/mp4"}

        async def body(self):
            return body

    class FakeRequest:
        async def get(self, url, timeout):
            assert "name=media-cdp" in url
            assert timeout == 180_000
            return FakeResponse()

    class FakeLocator:
        async def evaluate_all(self, _script):
            return [
                {
                    "src": "/download?name=media-cdp",
                    "href": "/edit/workflow-cdp",
                    "tile_id": "workflow-cdp",
                    "media_key": "media-cdp",
                }
            ]

    class FakePage:
        request = FakeRequest()

        async def goto(self, url, wait_until, timeout):
            assert url == "https://flow.google.com/project/project-cdp"
            assert wait_until == "domcontentloaded"
            assert timeout == 30_000

        async def wait_for_timeout(self, _ms):
            return None

        def locator(self, selector):
            assert selector == "video[src]"
            return FakeLocator()

        async def reload(self, **_kwargs):
            raise AssertionError("reload should not be needed")

        async def close(self):
            page_closed.append(True)

    class FakeContext:
        async def new_page(self):
            return FakePage()

    class FakeExistingChromeManager:
        def __init__(self, port_file):
            assert port_file == flow._chrome_port_file
            self.context = FakeContext()

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return None

    monkeypatch.setattr(
        recovery_module, "can_attach_existing_chrome", lambda _path: True
    )
    monkeypatch.setattr(
        recovery_module, "_ExistingChromeManager", FakeExistingChromeManager
    )
    monkeypatch.setattr(
        flow.vault,
        "load",
        lambda: (_ for _ in ()).throw(
            AssertionError("cookie vault must not be used for CDP recovery")
        ),
    )

    job = SimpleNamespace(
        job_id="job-cdp",
        workflow_id="workflow-cdp",
        media_id="media-cdp",
        resource_name=None,
        operation_name=None,
        raw={},
    )
    files = await flow._download_via_browser(
        "project-cdp",
        job,
        tmp_path / "renders",
    )

    assert page_closed == [True]
    assert files[0].name == "flow_media-cdp.mp4"
    assert files[0].read_bytes() == body
