from pathlib import Path
from types import SimpleNamespace

import pytest

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.flow_integration import (
    FlowCLIIntegration,
    FlowIntegrationError,
    gflow_transport,
)
from flow_story_studio.flow_integration import generation as generation_module
from flow_story_studio.models import AnalyzeRequest

SCRIPT = """
SCENE 1 — ROOM — NIGHT
A person crosses the room and stops beside the table.
"""


def _project():
    project = analyze_story(AnalyzeRequest(name="gflow live", original_text=SCRIPT))
    project.settings.provider = "google-flow"
    project.settings.video_model = "veo-3.1-lite-lower-priority"
    project.settings.aspect_ratio = "16:9"
    return project


def test_gflow_lower_priority_model_mapping() -> None:
    from gflow_cli.api.video import VideoModel

    assert gflow_transport._map_video_model(
        "veo-3.1-lite-lower-priority"
    ) is VideoModel.VEO_3_1_LITE_LOWER_PRIORITY


def test_gflow_veo_duration_is_omitted() -> None:
    from gflow_cli.api.video import VideoModel

    assert (
        gflow_transport._request_duration(
            VideoModel.VEO_3_1_LITE_LOWER_PRIORITY, 8
        )
        is None
    )
    assert gflow_transport._request_duration(VideoModel.OMNI_FLASH, 8) == 8


def test_gflow_migrated_ready_anchor_skips_hidden_duplicate(monkeypatch) -> None:
    from gflow_cli.api.transports import migrated_composer

    monkeypatch.setattr(migrated_composer, "READY_ANCHOR", ".settings-trigger-button")
    gflow_transport._apply_migrated_ui_compat()

    assert migrated_composer.READY_ANCHOR == ".settings-trigger-button:not([hidden])"


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_gflow_t2v_downloads_mp4_and_persists_identity(
    monkeypatch, tmp_path: Path
) -> None:
    from gflow_cli.api import client as client_module
    from gflow_cli.api.video import Mode, VideoModel

    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "gflow")
    profile = tmp_path / "gflow-profile"
    profile.mkdir()
    monkeypatch.setattr(
        gflow_transport, "resolve_gflow_profile_dir", lambda: profile
    )
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def generate_video(
            self,
            *,
            req,
            project_id,
            out_dir,
            poll_timeout_s,
            download,
            on_started,
        ):
            captured["req"] = req
            captured["project_id"] = project_id
            captured["poll_timeout_s"] = poll_timeout_s
            captured["download"] = download
            on_started(
                SimpleNamespace(
                    media_id="media-gflow-1",
                    project_id=project_id,
                    flow_operation_id="operation-gflow-1",
                )
            )
            video = Path(out_dir) / "gflow.mp4"
            video.write_bytes(b"0000ftyp" + b"x" * 2048)
            return SimpleNamespace(
                status=SimpleNamespace(
                    succeeded=True,
                    media_id="media-gflow-1",
                    error_message=None,
                    failure_reasons=(),
                    status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
                ),
                local_path=video,
                project_id=project_id,
                flow_operation_id="operation-gflow-1",
            )

    monkeypatch.setattr(client_module, "FlowApiClient", FakeClient)

    project = _project()
    project.flow_project_id = "5fa6b591-8d1c-4a59-9106-2b930a4661f2"
    scene = project.scenes[0]
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(
        flow,
        "_extract_last_frame",
        lambda *_args, **_kwargs: _async_value("frames/gflow-last.jpg"),
    )
    checkpoints: list[str] = []

    result = await flow.generate(
        project,
        scene,
        checkpoint=lambda _project, current: checkpoints.append(
            current.provider_job_id
        ),
    )

    req = captured["req"]
    assert req.mode is Mode.T2V
    assert req.model is VideoModel.VEO_3_1_LITE_LOWER_PRIORITY
    assert req.duration is None
    assert req.count == 1
    assert captured["download"] is True
    assert captured["project_id"] == project.flow_project_id
    assert scene.provider_job_id == "media-gflow-1"
    assert scene.upstream_media_id == "media-gflow-1"
    assert scene.upstream_workflow_id == "operation-gflow-1"
    assert result.job_id == "media-gflow-1"
    assert result.result_file.endswith("/gflow.mp4")
    assert len(checkpoints) >= 2


@pytest.mark.asyncio
async def test_gflow_reference_image_uses_i2v_start_frame(
    monkeypatch, tmp_path: Path
) -> None:
    from gflow_cli.api import client as client_module
    from gflow_cli.api.video import Mode

    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "gflow")
    profile = tmp_path / "gflow-profile"
    profile.mkdir()
    monkeypatch.setattr(
        gflow_transport, "resolve_gflow_profile_dir", lambda: profile
    )
    reference = tmp_path / "references" / "entities" / "char.png"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"png")
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def generate_video(
            self,
            *,
            req,
            project_id,
            out_dir,
            poll_timeout_s,
            download,
            on_started,
        ):
            captured["req"] = req
            on_started(
                SimpleNamespace(
                    media_id="media-i2v",
                    project_id=project_id,
                    flow_operation_id="operation-i2v",
                )
            )
            video = Path(out_dir) / "i2v.mp4"
            video.write_bytes(b"0000ftyp" + b"x" * 2048)
            return SimpleNamespace(
                status=SimpleNamespace(
                    succeeded=True,
                    media_id="media-i2v",
                    error_message=None,
                    failure_reasons=(),
                    status="MEDIA_GENERATION_STATUS_SUCCESSFUL",
                ),
                local_path=video,
                project_id=project_id,
                flow_operation_id="operation-i2v",
            )

    monkeypatch.setattr(client_module, "FlowApiClient", FakeClient)

    project = _project()
    project.flow_project_id = "5fa6b591-8d1c-4a59-9106-2b930a4661f2"
    scene = project.scenes[0]
    scene.reference_image = "references/entities/char.png"
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(
        flow,
        "_extract_last_frame",
        lambda *_args, **_kwargs: _async_value("frames/i2v-last.jpg"),
    )

    await flow.generate(project, scene)

    req = captured["req"]
    assert req.mode is Mode.I2V
    assert req.start_image == reference.resolve()


@pytest.mark.asyncio
async def test_explicit_gflow_transport_fails_closed_without_gflow(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "gflow")
    monkeypatch.setattr(gflow_transport, "gflow_available", lambda: False)
    project = _project()
    scene = project.scenes[0]
    flow = FlowCLIIntegration(tmp_path)

    with pytest.raises(FlowIntegrationError, match="không fallback"):
        await generation_module.generate(flow, project, scene)



@pytest.mark.asyncio
async def test_gflow_reference_image_generation_uses_image_transport(
    monkeypatch, tmp_path: Path
) -> None:
    from gflow_cli.api import client as client_module
    from gflow_cli.api.image import Aspect, Model

    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "gflow")
    profile = tmp_path / "gflow-profile"
    profile.mkdir()
    monkeypatch.setattr(
        gflow_transport, "resolve_gflow_profile_dir", lambda: profile
    )
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def generate_image(self, *, project_id, req, count):
            captured["project_id"] = project_id
            captured["req"] = req
            captured["count"] = count
            return [
                SimpleNamespace(
                    media_name="image-media-1",
                    fife_url="https://flow-content.google/example",
                )
            ]

        async def download_image(self, image, out_path):
            captured["image"] = image
            Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 128)
            return Path(out_path)

    monkeypatch.setattr(client_module, "FlowApiClient", FakeClient)
    flow = FlowCLIIntegration(tmp_path)

    relative = await flow.generate_reference_image(
        "flow-project-1",
        "CHAR_001",
        "canonical portrait, neutral background",
    )

    req = captured["req"]
    assert req.aspect is Aspect.SQUARE
    assert req.model is Model.GEM_PIX_2
    assert req.count == 1
    assert captured["count"] == 1
    assert captured["project_id"] == "flow-project-1"
    assert relative == "references/flow-project-1/entities/CHAR_001.png"
    assert (tmp_path / relative).is_file()


def test_gflow_profile_counts_as_configured(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "gflow")
    profile = tmp_path / "gflow-profile"
    profile.mkdir()
    monkeypatch.setattr(gflow_transport, "gflow_available", lambda: True)
    monkeypatch.setattr(gflow_transport, "gflow_enabled", lambda: True)
    monkeypatch.setattr(
        gflow_transport, "resolve_gflow_profile_dir", lambda: profile
    )

    flow = FlowCLIIntegration(tmp_path)

    assert flow.configured is True


@pytest.mark.asyncio
async def test_verify_gflow_profile_falls_back_to_migrated_ui(
    monkeypatch, tmp_path: Path
) -> None:
    from gflow_cli.auth import verification as verification_module
    from gflow_cli.auth.verification import FlowSessionOutcome

    profile = tmp_path / "gflow-profile"
    profile.mkdir()
    monkeypatch.setattr(
        gflow_transport, "resolve_gflow_profile_dir", lambda: profile
    )

    async def fake_upstream_verify(*_args, **_kwargs):
        return SimpleNamespace(
            outcome=FlowSessionOutcome.VERIFICATION_ERROR,
            detail="Could not verify the Flow session.",
        )

    async def fake_migrated_verify(_profile):
        return True, "Flow migrated UI session verified."

    monkeypatch.setattr(
        verification_module, "verify_flow_profile", fake_upstream_verify
    )
    monkeypatch.setattr(
        gflow_transport, "_verify_migrated_profile_ui", fake_migrated_verify
    )

    ok, detail, resolved = await gflow_transport.verify_gflow_profile()

    assert ok is True
    assert detail == "Flow migrated UI session verified."
    assert resolved == profile


@pytest.mark.asyncio
async def test_status_verifies_exact_gflow_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "gflow")
    profile = tmp_path / "gflow-profile"
    profile.mkdir()
    monkeypatch.setattr(gflow_transport, "gflow_available", lambda: True)
    monkeypatch.setattr(gflow_transport, "gflow_enabled", lambda: True)
    monkeypatch.setattr(
        gflow_transport, "resolve_gflow_profile_dir", lambda: profile
    )

    async def fake_verify():
        return True, "Flow app session verified.", profile

    monkeypatch.setattr(gflow_transport, "verify_gflow_profile", fake_verify)
    flow = FlowCLIIntegration(tmp_path)
    monkeypatch.setattr(flow, "_can_attach_existing_chrome", lambda: False)

    status = await flow.status(verify=True)

    assert status.configured is True
    assert status.authenticated is True
    assert status.transport == "gflow+flow.google.com"
    assert status.interactive_login_required is False


def test_gflow_mode_uses_gflow_chrome_profile(monkeypatch, tmp_path: Path) -> None:
    import gflow_cli.auth as gflow_auth

    profile = tmp_path / "profile_default"
    monkeypatch.setenv("FLOW_VIDEO_TRANSPORT", "gflow")
    monkeypatch.delenv("FLOW_CHROME_PROFILE", raising=False)
    monkeypatch.delenv("FLOW_CHROME_DEVTOOLS_ACTIVE_PORT", raising=False)
    monkeypatch.setattr(gflow_transport, "gflow_available", lambda: True)
    monkeypatch.setattr(gflow_transport, "gflow_enabled", lambda: True)
    monkeypatch.setattr(gflow_auth, "profile_dir", lambda _name="default": profile)

    flow = FlowCLIIntegration(tmp_path)

    assert flow._flow_chrome_profile == profile.resolve()
    assert flow._chrome_port_file == profile.resolve() / "DevToolsActivePort"
