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


@pytest.mark.asyncio
async def test_gflow_migrated_prompt_clears_stale_draft_before_insert() -> None:
    from gflow_cli.api.transports import migrated_composer

    gflow_transport._apply_migrated_ui_compat()
    events: list[tuple[str, object]] = []

    class FakeLocator:
        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def fill(self, value):
            events.append(("fill", value))

        async def click(self, **kwargs):
            events.append(("click", kwargs.get("timeout")))

    class FakeKeyboard:
        async def insert_text(self, value):
            events.append(("insert", value))

    class FakePage:
        keyboard = FakeKeyboard()

        def locator(self, selector):
            assert selector == migrated_composer.COMPOSER
            return FakeLocator()

        async def wait_for_timeout(self, value):
            events.append(("wait", value))

    await migrated_composer.MigratedComposer().send_prompt(
        FakePage(),
        "fresh migrated prompt",
    )

    assert events == [
        ("fill", ""),
        ("wait", 50),
        ("click", 5000),
        ("insert", "fresh migrated prompt"),
    ]


@pytest.mark.asyncio
async def test_windows_migrated_client_skips_cookie_preread(
    monkeypatch, tmp_path: Path
) -> None:
    from gflow_cli.api import client as client_module

    calls: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._preread_flow_cookies = {"stale": "value"}

        async def _preread_flow_session_cookies(self):
            calls.append("base")

    monkeypatch.setattr(client_module, "FlowApiClient", FakeClient)
    monkeypatch.setattr(gflow_transport.sys, "platform", "win32")

    client = gflow_transport._flow_api_client(
        profile_dir=tmp_path / "profile",
        headless=False,
        out_dir=tmp_path / "out",
    )
    await client._preread_flow_session_cookies()

    assert calls == []
    assert client._preread_flow_cookies == {}
    assert client.kwargs["profile_dir"] == tmp_path / "profile"


def test_gflow_i2v_staging_uses_unique_filename_and_preserves_bytes(tmp_path: Path) -> None:
    source = tmp_path / "reference.jpg"
    source.write_bytes(b"reference-bytes")

    first = gflow_transport._stage_unique_i2v_frame(
        tmp_path,
        "project-1",
        "SCENE_001",
        source,
    )
    second = gflow_transport._stage_unique_i2v_frame(
        tmp_path,
        "project-1",
        "SCENE_001",
        source,
    )

    assert first != second
    assert first.name.startswith("SCENE_001-")
    assert second.name.startswith("SCENE_001-")
    assert first.suffix == ".jpg"
    assert second.suffix == ".jpg"
    assert first.read_bytes() == b"reference-bytes"
    assert second.read_bytes() == b"reference-bytes"


def test_gflow_i2v_staging_cleanup_removes_attempt_file(tmp_path: Path) -> None:
    source = tmp_path / "reference.png"
    source.write_bytes(b"png-bytes")
    staged = gflow_transport._stage_unique_i2v_frame(
        tmp_path,
        "project-1",
        "SCENE_001",
        source,
    )

    gflow_transport._cleanup_staged_i2v_frame(staged, tmp_path)

    assert not staged.exists()
    assert not (tmp_path / ".gflow-staging").exists()


@pytest.mark.asyncio
async def test_gflow_migrated_upload_uses_stabilized_mouse_filechooser(
    monkeypatch, tmp_path: Path
) -> None:
    from gflow_cli.api.transports import migrated_composer

    media_id = "615ad30b-d653-40a7-b81e-37a96ddb5442"
    project_id = "5fa6b591-8d1c-4a59-9106-2b930a4661f2"
    image = tmp_path / "reference.jpg"
    image.write_bytes(b"jpg")

    monkeypatch.setattr(migrated_composer, "_rpcid", lambda _url: migrated_composer.UPLOAD_RPC)
    monkeypatch.setattr(migrated_composer, "_first_uuid", lambda _body: media_id)

    class FakeResponse:
        url = "https://flow.google.com/data/batchexecute?rpcids=maseQ"
        status = 200

        async def text(self):
            return "reply"

    class FakeChooser:
        files: list[str] = []

        async def set_files(self, value):
            self.files.append(value)
            await page.response_callback(FakeResponse())

    chooser = FakeChooser()

    class FakeChooserContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @property
        def value(self):
            return _async_value(chooser)

    class FakeLocator:
        def __init__(self, kind):
            self.kind = kind

        @property
        def first(self):
            return self

        async def count(self):
            return 1

        async def click(self, **_kwargs):
            return None

        async def wait_for(self, **_kwargs):
            return None

        async def bounding_box(self):
            return {"x": 10.0, "y": 20.0, "width": 100.0, "height": 40.0}

    class FakeMouse:
        clicks: list[tuple[float, float]] = []

        async def click(self, x, y):
            self.clicks.append((x, y))

    class FakeKeyboard:
        async def press(self, _key):
            return None

    class FakePage:
        mouse = FakeMouse()
        keyboard = FakeKeyboard()
        response_callback = None
        removed = False

        def on(self, event, callback):
            assert event == "response"
            self.response_callback = callback

        def remove_listener(self, event, callback):
            assert event == "response"
            assert callback is self.response_callback
            self.removed = True

        def locator(self, selector):
            if selector == migrated_composer.TOOLBAR_ADD:
                return FakeLocator("add")
            if selector == migrated_composer.UPLOAD_MENU_ITEM:
                return FakeLocator("upload")
            raise AssertionError(selector)

        async def wait_for_timeout(self, _ms):
            return None

        def expect_file_chooser(self, **_kwargs):
            return FakeChooserContext()

    page = FakePage()
    result = await gflow_transport._upload_via_toolbar_mouse_compat(
        object(), page, project_id, image
    )

    assert result == media_id
    assert page.mouse.clicks == [(60.0, 40.0)]
    assert chooser.files == [str(image)]
    assert page.removed is True


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
            captured["staged_exists_during"] = req.start_image.is_file()
            captured["staged_bytes"] = req.start_image.read_bytes()
            captured["staged_name"] = req.start_image.name
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
    assert req.start_image != reference.resolve()
    assert req.start_image.parent.name == scene.id
    assert req.start_image.name.startswith(f"{scene.id}-")
    assert req.start_image.suffix == ".png"
    assert captured["staged_exists_during"] is True
    assert captured["staged_bytes"] == reference.read_bytes()
    assert not req.start_image.exists()
    assert not (tmp_path / ".gflow-staging").exists()


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
async def test_gflow_reference_image_generation_uses_migrated_ui_transport(
    monkeypatch, tmp_path: Path
) -> None:
    from gflow_cli.api import client as client_module

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

        async def download(self, source, out_path):
            captured["source"] = source
            Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 128)
            return Path(out_path)

    async def fake_drive(client, project, prompt, *, model_name, timeout_s):
        captured["project"] = project
        captured["prompt"] = prompt
        captured["model_name"] = model_name
        captured["timeout_s"] = timeout_s
        return (
            "https://flow-content.google/image/"
            "92c92488-c530-4a64-8e44-43ad323cc8d4?Signature=redacted",
            "92c92488-c530-4a64-8e44-43ad323cc8d4",
        )

    monkeypatch.setattr(client_module, "FlowApiClient", FakeClient)
    monkeypatch.setattr(
        gflow_transport, "_drive_migrated_reference_image", fake_drive
    )
    flow = FlowCLIIntegration(tmp_path)
    project = _project()
    project.flow_project_id = "5fa6b591-8d1c-4a59-9106-2b930a4661f2"

    relative = await flow.generate_reference_image(
        project,
        "CHAR_001",
        "canonical portrait, neutral background",
    )

    assert captured["project"] is project
    assert captured["prompt"] == "canonical portrait, neutral background"
    assert captured["model_name"] == "nano-pro"
    assert str(captured["source"]).startswith("https://flow-content.google/image/")
    assert relative == f"references/{project.id}/entities/CHAR_001.png"
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
