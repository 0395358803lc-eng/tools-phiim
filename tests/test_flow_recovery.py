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
