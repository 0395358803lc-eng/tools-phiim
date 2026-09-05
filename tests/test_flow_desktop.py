import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from flow_story_studio.flow_integration import CookieVault, FlowCLIIntegration
from flow_story_studio.main import create_app
from flow_story_studio.models import AnalyzeRequest, FlowConnection, VideoSettings
from flow_story_studio.providers.base import RenderResult
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.scene_contracts import seal_scene_contract
from flow_story_studio.service import StudioService
from flow_story_studio.storage import ProjectStorage

TEXT = (
    "Một người phụ nữ bước vào căn phòng và đặt chiếc hộp lên bàn. "
    "Cô mở cửa sổ, nhìn ra đường rồi quay lại nhấc chiếc hộp lên."
)


class FakeFlow:
    configured = False

    async def status(self, *, verify: bool = False) -> FlowConnection:
        return FlowConnection(
            configured=self.configured,
            authenticated=self.configured and verify,
            transport="legacy-cookie" if self.configured else "none",
            cookie_count=8 if self.configured else 0,
            message="ready" if self.configured else "missing",
            flow_cli_available=True,
            browser_ready=True,
        )

    async def connect(self, cookie: str) -> FlowConnection:
        self.configured = bool(cookie)
        return await self.status(verify=True)

    def disconnect(self) -> None:
        self.configured = False

    async def generate(self, project: object, scene: object) -> RenderResult:
        return RenderResult(job_id="fake-flow", result_url="/video")


def test_cookie_vault_round_trip_uses_encrypted_bytes(tmp_path: Path) -> None:
    vault = CookieVault(tmp_path / "flow.cookies.bin")
    vault.save({"SID": "top-secret"}, [{"name": "SID", "value": "top-secret"}])
    assert b"top-secret" not in vault.path.read_bytes()
    cookies, raw = vault.load()
    assert cookies == {"SID": "top-secret"}
    assert raw and raw[0]["name"] == "SID"
    vault.clear()
    assert vault.load() == ({}, None)


def test_flow_cookie_is_shared_across_workspaces_and_legacy_is_migrated(
    tmp_path: Path,
) -> None:
    workspace_one = tmp_path / "workspace-one"
    workspace_two = tmp_path / "workspace-two"
    shared_credentials = tmp_path / "app-data" / "secrets"

    legacy = FlowCLIIntegration(workspace_one)
    legacy.vault.save({"SID": "legacy-secret"}, None)
    migrated = FlowCLIIntegration(workspace_one, credential_root=shared_credentials)
    assert migrated.vault.load()[0] == {"SID": "legacy-secret"}
    assert b"legacy-secret" not in migrated.vault.path.read_bytes()

    reopened_in_another_workspace = FlowCLIIntegration(
        workspace_two, credential_root=shared_credentials
    )
    assert reopened_in_another_workspace.configured is True
    assert reopened_in_another_workspace.vault.load()[0] == {"SID": "legacy-secret"}
    assert reopened_in_another_workspace.data_root == workspace_two.resolve()


def test_vendored_flow_client_import_path() -> None:
    from flow_cli._client import FlowClient

    assert FlowClient.__name__ == "FlowClient"


def test_current_flow_radix_tab_selection_profile(tmp_path: Path) -> None:
    import flow_cli._flow_ui as flow_ui

    FlowCLIIntegration(tmp_path)._apply_flow_ui_compatibility()
    assert any('[role="tab"]' in selector for selector in flow_ui.SELECTED_OPTION_TEMPLATES)
    assert getattr(flow_ui.FlowUI.select_model, "_studio_compat", False)


def test_flow_403_retries_headed_and_remembers_mode(tmp_path: Path) -> None:
    integration = FlowCLIIntegration(tmp_path)
    calls: list[bool] = []

    class FakeClient:
        async def generate_video(self, **kwargs: object) -> SimpleNamespace:
            headless = bool(kwargs["headless"])
            calls.append(headless)
            if len(calls) == 1:
                raise RuntimeError(
                    "Generation request failed with upstream HTTP 403 "
                    "at video:batchAsyncGenerateVideoText"
                )
            return SimpleNamespace(job_id="headed-success")

    result = asyncio.run(integration._generate_video(FakeClient(), prompt="safe"))
    assert result.job_id == "headed-success"
    assert calls == [True, False]
    assert integration._force_headed_browser is True


def test_gflow_installed_without_profile_requests_login(tmp_path: Path) -> None:
    integration = FlowCLIIntegration(tmp_path)
    integration.gflow_executable = str(tmp_path / "gflow.cmd")

    status = asyncio.run(integration.status())

    assert status.configured is False
    assert status.transport == "none"
    assert status.gflow_available is True
    assert status.gflow_profile_ready is False
    assert "gflow auth login" in status.message


def test_gflow_profile_status_reports_primary_transport(tmp_path: Path) -> None:
    integration = FlowCLIIntegration(tmp_path)
    integration.gflow_executable = str(tmp_path / "gflow.cmd")
    profile = integration._gflow_profile_dir()
    profile.mkdir(parents=True)

    status = asyncio.run(integration.status())

    assert status.configured is True
    assert status.transport == "gflow"
    assert status.cookie_count == 0


def test_flow_connection_and_reference_routes(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    flow = FakeFlow()
    app = create_app(storage, flow_integration=flow)  # type: ignore[arg-type]
    with TestClient(app) as client:
        initial = client.get("/api/video/flow/status").json()
        assert initial["configured"] is False
        assert initial["transport"] == "none"
        connected = client.post("/api/video/flow/connect", json={"cookie": "SID=valid-cookie"})
        assert connected.status_code == 200
        assert connected.json()["authenticated"] is True
        assert connected.json()["transport"] == "legacy-cookie"

        project = client.post(
            "/api/projects/analyze",
            json={
                "name": "Flow Desktop",
                "original_text": TEXT,
                "settings": {"provider": "google-flow"},
            },
        ).json()
        scene_id = project["scenes"][0]["id"]
        uploaded = client.post(
            f"/api/projects/{project['id']}/scenes/{scene_id}/reference",
            content=b"\x89PNG\r\n\x1a\nmock",
            headers={"Content-Type": "image/png"},
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["scenes"][0]["reference_image"].endswith("-manual.png")

        disconnected = client.delete("/api/video/flow")
        assert disconnected.json()["configured"] is False
        blocked = client.post(
            f"/api/projects/{project['id']}/generate", json={"scene_ids": [scene_id]}
        )
        assert blocked.status_code == 409


def test_embedded_flow_generation_contract(tmp_path: Path, monkeypatch: object) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(
        AnalyzeRequest(
            name="Embedded Flow",
            original_text=TEXT,
            settings=VideoSettings(provider="google-flow", video_model="veo-3.1-fast"),
        )
    )
    integration = FlowCLIIntegration(tmp_path)
    integration.vault.save({"SID": "secret"}, None)
    captured: dict[str, object] = {}

    class FakeClient:
        async def create_project(self, title: str, media_type: str) -> str:
            captured["project"] = (title, media_type)
            return "upstream-project"

        async def generate_video(self, **kwargs: object) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(job_id="job-1", is_success=False, raw={})

        async def wait_for_video(self, job: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(job_id="job-1", is_success=True, raw={"done": True})

        async def download(self, workflow: object, dest_dir: Path) -> list[Path]:
            video = dest_dir / "result.mp4"
            video.write_bytes(b"video")
            return [video]

    monkeypatch.setattr(integration, "_client", lambda *args, **kwargs: FakeClient())  # type: ignore[attr-defined]

    async def fake_last_frame(*args: object) -> str:
        return "references/project/last.jpg"

    monkeypatch.setattr(integration, "_extract_last_frame", fake_last_frame)  # type: ignore[attr-defined]
    checkpoints: list[tuple[str, str]] = []
    result = asyncio.run(
        integration.generate(
            project,
            project.scenes[0],
            checkpoint=lambda current, scene: checkpoints.append(
                (current.flow_project_id, scene.provider_job_id)
            ),
        )
    )
    assert captured["model"] == "veo-3.1-fast"
    assert captured["headless"] is True
    assert result.upstream_project_id == "upstream-project"
    assert result.result_file.endswith("result.mp4")
    assert result.last_frame_file.endswith("last.jpg")
    assert checkpoints[0] == ("upstream-project", "")
    assert checkpoints[-1] == ("upstream-project", "job-1")


def test_flow_browser_download_fallback_and_candidate_identity(
    tmp_path: Path, monkeypatch: object
) -> None:
    project = StudioService(ProjectStorage(tmp_path / "projects")).analyze(
        AnalyzeRequest(
            name="Fallback download",
            original_text=TEXT,
            settings=VideoSettings(provider="google-flow"),
        )
    )
    integration = FlowCLIIntegration(tmp_path)
    integration.vault.save({"SID": "secret"}, None)

    class FakeClient:
        async def create_project(self, title: str, media_type: str) -> str:
            return "project-1"

        async def generate_video(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                job_id="job-1",
                is_success=True,
                workflow_id="workflow-exact",
                media_id=None,
                resource_name=None,
                raw={"preview": "image-only"},
            )

        async def download(self, workflow: object, dest_dir: Path) -> list[Path]:
            return []

    integration._client = lambda *args, **kwargs: FakeClient()  # type: ignore[method-assign]
    fallback_calls: list[str] = []

    async def fake_browser_download(project_id: str, job: object, output: Path) -> list[Path]:
        fallback_calls.append(project_id)
        output.mkdir(parents=True, exist_ok=True)
        video = output / "browser-recovered.mp4"
        video.write_bytes(b"video")
        return [video]

    async def fake_last_frame(*args: object) -> str:
        return "references/project/last.jpg"

    monkeypatch.setattr(integration, "_download_via_browser", fake_browser_download)
    monkeypatch.setattr(integration, "_extract_last_frame", fake_last_frame)
    result = asyncio.run(integration.generate(project, project.scenes[0]))
    assert fallback_calls == ["project-1"]
    assert result.result_file.endswith("browser-recovered.mp4")
    assert project.scenes[0].upstream_workflow_id == "workflow-exact"

    candidates = [
        {"src": "first", "tile_id": "other", "media_key": "media-a"},
        {"src": "second", "tile_id": "workflow-exact", "media_key": "media-b"},
    ]
    selected = integration._select_video_candidate(candidates, {"workflow-exact"})
    assert selected and selected["src"] == "second"
    assert integration._select_video_candidate(candidates, {"unknown"}) is None


def test_generate_all_keeps_accepted_scenes(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(
        AnalyzeRequest(name="Resume queue", original_text=TEXT)
    )
    project.scenes[0].status = "Accepted"
    project.scenes[0].progress = 100
    project.scenes[0].acceptance.status = "Accepted"
    project.scenes[0].acceptance.score = 100
    storage.save(project)
    queue = RenderQueue(storage, FakeFlow())  # type: ignore[arg-type]

    async def run() -> None:
        queued = await queue.enqueue(project.id, [])
        assert queued.scenes[0].status == "Accepted"
        assert queued.scenes[0].progress == 100
        await queue.shutdown()

    asyncio.run(run())


def test_pause_stops_pending_scenes_without_interrupting_active_render(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(AnalyzeRequest(name="Pause queue", original_text=TEXT))
    if len(project.scenes) < 2:
        pytest.skip("Need at least two scenes for pause semantics")
    project.scenes[0].status = "Generating"
    project.scenes[1].status = "Waiting"
    storage.save(project)
    queue = RenderQueue(storage, FakeFlow())  # type: ignore[arg-type]
    queue.pause(project.id)
    paused = storage.get(project.id)
    assert paused is not None
    assert paused.scenes[0].status == "Generating"
    assert paused.scenes[1].status == "Paused"


def test_reference_path_is_sandboxed_to_reference_directory(tmp_path: Path) -> None:
    integration = FlowCLIIntegration(tmp_path)
    references = tmp_path / "references" / "project"
    references.mkdir(parents=True)
    allowed = references / "frame.png"
    allowed.write_bytes(b"image")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"image")

    assert integration._reference_path(allowed.as_posix()) == str(allowed.resolve())
    assert integration._reference_path(outside.as_posix()) is None
    assert integration._reference_path(r"C:\Windows\win.ini") is None


def test_reference_upload_rejects_mismatched_image_bytes(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    app = create_app(storage, flow_integration=FakeFlow())  # type: ignore[arg-type]
    with TestClient(app) as client:
        project = client.post(
            "/api/projects/analyze",
            json={"name": "Invalid image", "original_text": TEXT, "settings": {}},
        ).json()
        scene_id = project["scenes"][0]["id"]
        response = client.post(
            f"/api/projects/{project['id']}/scenes/{scene_id}/reference",
            content=b"not-a-png",
            headers={"Content-Type": "image/png"},
        )
        assert response.status_code == 415


def test_render_queue_does_not_chain_reference_across_scene_cut(tmp_path: Path) -> None:
    from flow_story_studio.engines.segmenter import SCENE_CONTEXT_PREFIX

    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(
        AnalyzeRequest(name="Reference cut", original_text=TEXT)
    )
    if len(project.scenes) < 2:
        pytest.skip("Need at least two scenes")
    project.settings.provider = "google-flow"
    project.scenes[1].source_text = f"{SCENE_CONTEXT_PREFIX}CUT [END CONTEXT]\nnew beat"
    seal_scene_contract(project.scenes[1])
    storage.save(project)
    seen_references: list[str] = []

    class CaptureFlow:
        configured = True

        async def generate(self, current: object, scene: object, checkpoint=None) -> RenderResult:
            seen_references.append(scene.reference_image)
            return RenderResult(
                job_id=f"job-{scene.id}",
                result_url="/video",
                result_file=f"renders/{project.id}/{scene.id}/result.mp4",
                last_frame_file=f"references/{project.id}/{scene.id}-last.jpg",
            )

    queue = RenderQueue(storage, CaptureFlow())  # type: ignore[arg-type]

    async def run() -> None:
        await queue.enqueue(project.id, [project.scenes[0].id, project.scenes[1].id])
        await queue._queues[project.id].join()
        await queue.shutdown()

    asyncio.run(run())
    assert seen_references[:2] == ["", ""]


def test_explicit_rerender_does_not_recover_stale_upstream_job(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = StudioService(storage).analyze(
        AnalyzeRequest(name="Fresh rerender", original_text=TEXT)
    )
    project.settings.provider = "google-flow"
    scene = project.scenes[0]
    scene.provider_job_id = "old-job"
    scene.upstream_project_id = "old-project"
    scene.upstream_workflow_id = "old-workflow"
    scene.upstream_media_id = "old-media"
    scene.upstream_resource_name = "old-resource"
    storage.save(project)
    captured: list[tuple[str, str, str, str, str]] = []

    class CaptureFlow:
        configured = True

        async def generate(
            self, current: object, current_scene: object, checkpoint=None
        ) -> RenderResult:
            captured.append(
                (
                    current_scene.provider_job_id,
                    current_scene.upstream_project_id,
                    current_scene.upstream_workflow_id,
                    current_scene.upstream_media_id,
                    current_scene.upstream_resource_name,
                )
            )
            return RenderResult(job_id="new-job", result_url="/video")

    queue = RenderQueue(storage, CaptureFlow())  # type: ignore[arg-type]

    async def run() -> None:
        await queue.enqueue(project.id, [scene.id], force_rerender=True)
        await queue._queues[project.id].join()
        await queue.shutdown()

    asyncio.run(run())
    assert captured == [("", "", "", "", "")]
