from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.flow_integration import FlowCLIIntegration, FlowIntegrationError
from flow_story_studio.models import AnalyzeRequest
from flow_story_studio.providers.base import RenderResult
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.storage import ProjectStorage

SCRIPT = """
SCENE 1 — ROOM — NIGHT
A person crosses the room and stops beside the table.
"""


async def test_retry_preserves_existing_flow_job_identity_unless_forced(tmp_path):
    project = analyze_story(AnalyzeRequest(name="retry identity", original_text=SCRIPT))
    scene = project.scenes[0]
    scene.status = "Failed"
    scene.provider_job_id = "job-existing"
    scene.upstream_project_id = "project-existing"
    scene.upstream_workflow_id = "workflow-existing"
    scene.upstream_media_id = "media-existing"
    scene.upstream_resource_name = "resource-existing"

    storage = ProjectStorage(tmp_path / "projects")
    storage.save(project)

    class FakeFlow:
        configured = True

        async def generate(self, _project, _scene, checkpoint=None):
            return RenderResult(job_id="unexpected")

    queue = RenderQueue(storage, FakeFlow())  # type: ignore[arg-type]
    queued = await queue.enqueue(project.id, [scene.id])
    retried = queued.scenes[0]
    assert retried.provider_job_id == "job-existing"
    assert retried.upstream_project_id == "project-existing"
    assert retried.upstream_workflow_id == "workflow-existing"
    assert retried.upstream_media_id == "media-existing"
    assert retried.upstream_resource_name == "resource-existing"
    await queue.shutdown()

    queue = RenderQueue(storage, FakeFlow())  # type: ignore[arg-type]
    queued = await queue.enqueue(project.id, [scene.id], force_rerender=True)
    forced = queued.scenes[0]
    assert forced.provider_job_id == ""
    assert forced.upstream_project_id == ""
    assert forced.upstream_workflow_id == ""
    assert forced.upstream_media_id == ""
    assert forced.upstream_resource_name == ""
    await queue.shutdown()


async def test_recovery_failure_never_falls_through_to_new_submission(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "legacy")
    project = analyze_story(AnalyzeRequest(name="recover existing", original_text=SCRIPT))
    scene = project.scenes[0]
    scene.provider_job_id = "job-existing"
    scene.upstream_project_id = "project-existing"

    flow = FlowCLIIntegration(tmp_path)

    async def fail_download(*_args, **_kwargs):
        raise RuntimeError("temporary download failure")

    monkeypatch.setattr(flow, "_download_via_browser", fail_download)

    try:
        await flow._recover_submitted(project, scene)
    except FlowIntegrationError as exc:
        assert "no new generation was submitted" in str(exc)
    else:
        raise AssertionError("Recovery failure must not submit a replacement Flow job")


async def test_incomplete_existing_job_identity_is_fail_closed(tmp_path):
    project = analyze_story(AnalyzeRequest(name="incomplete identity", original_text=SCRIPT))
    scene = project.scenes[0]
    scene.provider_job_id = "job-existing"

    flow = FlowCLIIntegration(tmp_path)

    try:
        await flow._recover_submitted(project, scene)
    except FlowIntegrationError as exc:
        assert "refusing to submit a duplicate" in str(exc)
    else:
        raise AssertionError("Incomplete upstream identity must fail closed")


async def test_download_completed_falls_back_to_browser_when_cli_returns_no_mp4(
    tmp_path, monkeypatch
):
    flow = FlowCLIIntegration(tmp_path)
    output = tmp_path / "renders"
    output.mkdir()
    preview = output / "preview.png"
    preview.write_bytes(b"png")
    recovered = output / "recovered.mp4"
    recovered.write_bytes(b"0" * 8 + b"ftyp" + b"0" * 2048)

    class FakeClient:
        async def download(self, _raw, dest_dir):
            assert dest_dir == output
            return [preview]

    async def browser_fallback(project_id, _job, target):
        assert project_id == "project-1"
        assert target == output
        return [recovered]

    monkeypatch.setattr(flow, "_download_via_browser", browser_fallback)
    completed = type("Completed", (), {"raw": {}})()

    files = await flow._download_completed(
        FakeClient(), completed, output, "project-1"
    )

    assert files == [recovered]


async def test_browser_recovery_writes_valid_exact_mp4(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from flow_cli import _browser

    flow = FlowCLIIntegration(tmp_path)
    flow._chrome_port_file = tmp_path / "missing-DevToolsActivePort"
    monkeypatch.setattr(flow.vault, "load", lambda: ({"SID": "x"}, None))
    payload = b"0000ftyp" + b"x" * 2048

    class FakeResponse:
        status = 200
        headers = {"content-type": "video/mp4"}

        async def body(self):
            return payload

    class FakeRequest:
        async def get(self, url, timeout):
            assert "media-123" in url
            assert timeout == 180_000
            return FakeResponse()

    class FakeLocator:
        async def evaluate_all(self, _script):
            return [
                {
                    "src": "/api/media?name=media-123",
                    "href": "/fx/tools/flow/project/p/edit/media-123",
                    "tile_id": "media-123",
                    "media_key": "media-123",
                }
            ]

    class FakePage:
        request = FakeRequest()

        async def goto(self, url, wait_until, timeout):
            assert url.endswith("/project/project-1")
            assert wait_until == "domcontentloaded"
            assert timeout == 30_000

        async def wait_for_timeout(self, _ms):
            return None

        def locator(self, selector):
            assert selector == "video[src]"
            return FakeLocator()

        async def reload(self, **_kwargs):
            raise AssertionError("reload should not be needed")

    class FakeBrowser:
        page = FakePage()

    class FakeBrowserManager:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return FakeBrowser()

        async def __aexit__(self, _exc_type, _exc, _tb):
            return None

    monkeypatch.setattr(_browser, "BrowserManager", FakeBrowserManager)
    job = SimpleNamespace(
        workflow_id="media-123",
        media_id=None,
        resource_name=None,
        operation_name=None,
        raw={},
    )

    files = await flow._download_via_browser(
        "project-1", job, tmp_path / "out"
    )

    assert len(files) == 1
    assert files[0].is_file()
    assert files[0].read_bytes() == payload
    assert files[0].name == "flow_media-123.mp4"
