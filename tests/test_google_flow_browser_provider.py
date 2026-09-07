from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.film.subclip_planner import (
    SubclipPlanningError,
    action_beats,
    plan_flow_subclips,
)
from flow_story_studio.google_flow_browser_worker import (
    FlowAsset,
    GoogleFlowBrowserError,
    GoogleFlowBrowserWorker,
    classify_flow_generation_failure,
)
from flow_story_studio.main import create_app
from flow_story_studio.models import AnalyzeRequest
from flow_story_studio.providers.base import RenderResult
from flow_story_studio.providers.google_flow_browser import GoogleFlowBrowserProvider
from flow_story_studio.providers.registry import ProviderRegistry
from flow_story_studio.storage import ProjectStorage


def _project_and_scene():
    project = analyze_story(
        AnalyzeRequest(
            name="flow",
            original_text=(
                "A man sits at a table. His phone vibrates. He looks at it. "
                "A voice warns him not to leave. He raises the phone. "
                "He asks where she is. The call disconnects. A knock sounds at the door."
            ),
        )
    )
    return project, project.scenes[0]


def test_subclip_planner_preserves_beat_ownership_and_duration() -> None:
    _project, scene = _project_and_scene()
    scene.duration = 14
    scene.action = (
        "Rain taps the window. The man sits at the table. His phone vibrates. "
        "The display shows an incoming call. He looks at the phone. "
        "A voice warns him not to leave. He raises the phone. "
        "The call disconnects. A knock sounds at the door."
    )

    source_beats = action_beats(scene.action)
    plans = plan_flow_subclips(scene)

    assert len(plans) == 2
    assert sum(item.semantic_duration for item in plans) == 14
    assert all(4 <= item.semantic_duration <= 10 for item in plans)
    assert all(item.flow_duration in {4, 6, 8, 10} for item in plans)
    assert [beat for item in plans for beat in item.beats] == source_beats
    assert plans[0].end_second == plans[1].start_second
    assert plans[-1].end_second == 14

    for plan in plans:
        for beat in plan.beats:
            assert beat in plan.prompt
        assert "Do not replay a completed beat" in plan.prompt


def test_subclip_planner_fails_closed_when_scene_has_no_separable_beats() -> None:
    _project, scene = _project_and_scene()
    scene.duration = 14
    scene.action = "One indivisible action without a sentence boundary"
    with pytest.raises(SubclipPlanningError):
        plan_flow_subclips(scene)


def test_google_flow_duration_rounds_up_only_within_single_clip() -> None:
    assert GoogleFlowBrowserWorker._flow_duration(4) == 4
    assert GoogleFlowBrowserWorker._flow_duration(7) == 8
    assert GoogleFlowBrowserWorker._flow_duration(9) == 10
    with pytest.raises(GoogleFlowBrowserError):
        GoogleFlowBrowserWorker._flow_duration(11)


class _FakeWorker:
    def __init__(self, root: Path) -> None:
        self.data_root = root
        self.video_calls = 0
        self.image_calls: list[dict] = []

    def configured(self) -> bool:
        return True

    def health(self) -> dict[str, object]:
        return {
            "ok": True,
            "configured": True,
            "provider": "google-flow-browser",
            "models": ["Veo 3.1 - Lite [Lower Priority]"],
            "image_models": ["Nano Banana 2"],
            "capabilities": {
                "video_resolutions": ["720p"],
                "video_aspect_ratios": ["16:9"],
                "video_durations": [4, 6, 8, 10],
            },
        }

    def generate_video(self, project, scene) -> FlowAsset:
        self.video_calls += 1
        relative = f"renders/{project.id}/{scene.id}.mp4"
        return FlowAsset(
            project_id="flow-project-1",
            media_id="media-video-1",
            label="video asset",
            kind="video",
            result_file=relative,
        )

    def generate_image(
        self,
        project,
        *,
        prompt,
        output_token,
        model="Nano Banana 2",
        ingredient_files=None,
    ) -> FlowAsset:
        self.image_calls.append(
            {
                "prompt": prompt,
                "output_token": output_token,
                "model": model,
                "ingredients": list(ingredient_files or []),
            }
        )
        target = (
            self.data_root
            / "references"
            / project.id
            / "generated"
            / f"{output_token}.jpg"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8\xfffake-jpeg")
        return FlowAsset(
            project_id="flow-project-1",
            media_id=f"media-{output_token}",
            label=output_token,
            kind="image",
            result_file=target.relative_to(self.data_root).as_posix(),
        )


@pytest.mark.asyncio
async def test_provider_maps_upstream_evidence() -> None:
    project, scene = _project_and_scene()
    worker = _FakeWorker(Path.cwd())
    provider = GoogleFlowBrowserProvider(worker)  # type: ignore[arg-type]

    result = await provider.generate(project, scene)

    assert isinstance(result, RenderResult)
    assert result.upstream_project_id == "flow-project-1"
    assert result.upstream_media_id == "media-video-1"
    assert result.upstream_resource_name == "video asset"
    assert project.provider_project_id == "flow-project-1"


@pytest.mark.asyncio
async def test_scene_image_generation_reuses_start_as_target_ingredient(tmp_path: Path) -> None:
    project, scene = _project_and_scene()
    project.settings.image_model = "Nano Banana Pro"
    scene.image_plan.status = "Ready"
    scene.image_plan.start_frame_strategy = "canonical_reanchor"
    scene.image_plan.start_frame_prompt = "start prompt"
    scene.image_plan.target_frame_prompt = "target prompt"
    worker = _FakeWorker(tmp_path)
    provider = GoogleFlowBrowserProvider(worker)  # type: ignore[arg-type]

    result = await provider.generate_scene_images(project, scene)

    assert len(worker.image_calls) == 2
    assert {call["model"] for call in worker.image_calls} == {"Nano Banana Pro"}
    assert result["generated_start_frame"].endswith(f"{scene.id}-start.jpg")
    assert result["generated_target_frame"].endswith(f"{scene.id}-target.jpg")
    target_call = worker.image_calls[1]
    start_path = tmp_path / result["generated_start_frame"]
    assert start_path in target_call["ingredients"]


class _FakeGoogleProvider(GoogleFlowBrowserProvider):
    requires_master_gate = False

    def __init__(self, root: Path) -> None:
        self.root = root

    def is_configured(self) -> bool:
        return True

    async def health(self) -> dict[str, object]:
        return {
            "ok": True,
            "configured": True,
            "provider": "google-flow-browser",
            "models": ["Veo 3.1 - Lite [Lower Priority]"],
            "image_models": ["Nano Banana 2"],
            "capabilities": {
                "video_resolutions": ["720p"],
                "video_aspect_ratios": ["16:9"],
                "video_durations": [4, 6, 8, 10],
                "video_input_modes": ["Frames", "Ingredients"],
            },
        }

    async def generate(self, project, scene) -> RenderResult:
        raise AssertionError("video generation is not part of this API test")

    async def generate_reference_image(
        self,
        project,
        reference_id: str,
        prompt: str,
        *,
        ingredient_files=None,
    ) -> str:
        assert reference_id.startswith("VIS-")
        assert "LOCKED SPECIFICATION" in prompt
        target = (
            self.root
            / "references"
            / project.id
            / "masters"
            / f"{reference_id}.jpg"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8\xffmaster")
        return target.relative_to(self.root).as_posix()

    async def generate_scene_images(self, project, scene) -> dict[str, str]:
        start = self.root / "references" / project.id / f"{scene.id}-start.jpg"
        target = self.root / "references" / project.id / f"{scene.id}-target.jpg"
        start.parent.mkdir(parents=True, exist_ok=True)
        start.write_bytes(b"\xff\xd8\xffstart")
        target.write_bytes(b"\xff\xd8\xfftarget")
        return {
            "generated_start_frame": start.relative_to(self.root).as_posix(),
            "generated_target_frame": target.relative_to(self.root).as_posix(),
            "upstream_project_id": "flow-project-api",
            "upstream_media_id": "flow-image-api",
        }


def test_render_status_and_scene_image_api(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    registry = ProviderRegistry()
    registry.register("google-flow-browser", _FakeGoogleProvider(tmp_path))
    app = create_app(storage, provider_registry=registry)

    with TestClient(app) as client:
        status = client.get("/api/render/status")
        assert status.status_code == 200
        payload = status.json()
        assert payload["configured"] is True
        assert "google-flow-browser" in payload["configured_providers"]
        detail = payload["provider_details"]["google-flow-browser"]
        assert detail["image_models"] == ["Nano Banana 2"]

        created = client.post(
            "/api/projects/analyze",
            json={"name": "image api", "original_text": "A man waits by a window."},
        )
        assert created.status_code == 201
        project = created.json()
        project_id = project["id"]
        scene_id = project["scenes"][0]["id"]

        configured = client.patch(
            f"/api/projects/{project_id}/video-settings",
            json={
                "provider": "google-flow-browser",
                "video_model": "Veo 3.1 - Lite [Lower Priority]",
                "resolution": "720p",
            },
        )
        assert configured.status_code == 200

        invalid_image_model = client.patch(
            f"/api/projects/{project_id}/image-settings",
            json={"image_model": "Imaginary Model"},
        )
        assert invalid_image_model.status_code == 422

        image_configured = client.patch(
            f"/api/projects/{project_id}/image-settings",
            json={"image_model": "Nano Banana 2"},
        )
        assert image_configured.status_code == 200
        assert image_configured.json()["settings"]["image_model"] == "Nano Banana 2"

        generated = client.post(
            f"/api/projects/{project_id}/scenes/{scene_id}/images/generate"
        )
        assert generated.status_code == 200
        scene = generated.json()["scenes"][0]
        assert scene["image_plan"]["status"] == "Generated"
        assert scene["image_plan"]["generated_start_frame"]
        assert scene["image_plan"]["generated_target_frame"]
        assert scene["upstream_project_id"] == "flow-project-api"

        start = client.get(
            f"/api/projects/{project_id}/scenes/{scene_id}/image/start"
        )
        target = client.get(
            f"/api/projects/{project_id}/scenes/{scene_id}/image/target"
        )
        assert start.status_code == 200
        assert target.status_code == 200
        assert start.headers["content-type"].startswith("image/jpeg")
        assert target.headers["content-type"].startswith("image/jpeg")


def test_master_generation_with_fake_google_provider_unlocks_image_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeXKiro:
        configured = True

        def set_checkpoint_root(self, _root) -> None:
            return None

    async def approve_master(
        _self,
        reference,
        relative_path: str,
        *,
        model_id: str = "",
    ):
        assert reference.id.startswith("VIS-")
        assert relative_path.endswith(".jpg")
        assert model_id == "vision-test"
        return 96, []

    monkeypatch.setattr(
        "flow_story_studio.reference_manager.VisualQCAnalyzer.inspect_reference",
        approve_master,
    )

    storage = ProjectStorage(tmp_path / "projects")
    provider = _FakeGoogleProvider(tmp_path)
    registry = ProviderRegistry()
    registry.register("google-flow-browser", provider)
    app = create_app(
        storage,
        xkiro_client=FakeXKiro(),  # type: ignore[arg-type]
        provider_registry=registry,
        reference_provider=provider,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/projects/analyze",
            json={
                "name": "master pipeline",
                "original_text": (
                    "A man waits beside a window in a small apartment. "
                    "His phone rests on a wooden table while rain falls outside."
                ),
                "settings": {
                    "vision_model": "vision-test",
                    "image_model": "Nano Banana 2",
                },
            },
        )
        assert created.status_code == 201
        project = created.json()
        project_id = project["id"]
        scene = project["scenes"][0]
        assert scene["image_plan"]["status"] == "Blocked"

        required_ids = list(scene["image_plan"]["character_reference_ids"])
        if scene["image_plan"]["location_reference_id"]:
            required_ids.append(scene["image_plan"]["location_reference_id"])
        required_ids.extend(scene["image_plan"]["prop_reference_ids"])
        required_ids = list(dict.fromkeys(required_ids))
        assert required_ids

        for index, reference_id in enumerate(required_ids):
            response = client.post(
                f"/api/projects/{project_id}/visual-references/{reference_id}/generate"
            )
            assert response.status_code == 200
            project = response.json()
            reference = next(
                item
                for item in project["visual_bible"]["references"]
                if item["id"] == reference_id
            )
            assert reference["status"] == "approved"
            assert reference["vision_score"] == 96
            assert reference["vision_model"] == "vision-test"
            assert reference["vision_issues"] == []
            assert reference["approved_reference"].endswith(f"{reference_id}.jpg")
            master_file = tmp_path / reference["approved_reference"]
            assert master_file.is_file()

            current_scene = next(
                item for item in project["scenes"] if item["id"] == scene["id"]
            )
            if index < len(required_ids) - 1:
                assert current_scene["image_plan"]["status"] == "Blocked"

        final_scene = next(
            item for item in project["scenes"] if item["id"] == scene["id"]
        )
        assert final_scene["image_plan"]["status"] == "Ready"
        assert all(
            final_scene["image_plan"]["reference_status"][reference_id] == "approved"
            for reference_id in required_ids
        )
        assert len(final_scene["image_plan"]["approved_reference_images"]) == len(
            required_ids
        )


class _ModelLocator:
    def __init__(self, items=None, *, count_override=None):
        self.items = list(items or [])
        self._count_override = count_override
        self.clicked = False

    @property
    def first(self):
        return self

    def count(self):
        if self._count_override is not None:
            return self._count_override
        return len(self.items)

    def click(self, *args, **kwargs):
        self.clicked = True

    def filter(self, *, has_text=None, **kwargs):
        if has_text is None:
            return self
        matches = [item for item in self.items if has_text in item]
        return _ModelLocator(matches)

    def nth(self, index):
        return _ModelLocator([self.items[index]])

    def inner_text(self):
        return self.items[0] if self.items else ""


class _ModelPage:
    def __init__(self):
        self.selector = _ModelLocator(["selector"])
        self.menu = _ModelLocator(["🍌 Nano Banana Pro", "🍌 Nano Banana 2"])
        self.selected = None

    def locator(self, selector):
        if selector == 'button[aria-label="Select model family"]:visible':
            return self.selector
        if selector == '[role="menuitem"]:visible':
            return self.menu
        raise AssertionError(selector)

    def get_by_role(self, role, *, name=None, exact=None):
        assert role == "menuitem"
        exact_matches = [item for item in self.menu.items if item == name]
        return _ModelLocator(exact_matches)

    def wait_for_timeout(self, _milliseconds):
        return None

    class _Keyboard:
        def press(self, _key):
            return None

    keyboard = _Keyboard()


def test_select_model_tolerates_google_flow_icon_prefix() -> None:
    worker = object.__new__(GoogleFlowBrowserWorker)
    page = _ModelPage()

    worker._select_model(page, "Nano Banana Pro")

    assert page.selector.clicked is True


class _MediaIdLocator:
    def __init__(self, media_id: str) -> None:
        self.media_id = media_id

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.media_id else 0

    def get_attribute(self, name):
        assert name == "data-media-id"
        return self.media_id


class _MediaTile:
    def __init__(self, label: str, media_id: str) -> None:
        self.label = label
        self.media_id = media_id

    def locator(self, selector):
        assert selector == "[data-media-id]"
        return _MediaIdLocator(self.media_id)

    def get_attribute(self, name):
        assert name == "aria-label"
        return self.label


class _MediaTiles:
    def __init__(self, tiles) -> None:
        self.tiles = list(tiles)

    def count(self):
        return len(self.tiles)

    def nth(self, index):
        return self.tiles[index]


def test_new_tile_detection_uses_media_id_when_labels_repeat() -> None:
    worker = object.__new__(GoogleFlowBrowserWorker)
    duplicate_label = "Woman sitting by apartment window"
    old_tile = _MediaTile(duplicate_label, "media-old")
    new_tile = _MediaTile(duplicate_label, "media-new")
    previous_media_ids = worker._tile_media_ids(_MediaTiles([old_tile]))

    result = worker._new_tile_by_media_id(
        _MediaTiles([new_tile, old_tile]),
        previous_media_ids,
    )

    assert result is new_tile
    assert result.get_attribute("aria-label") == duplicate_label
    assert worker._media_id_from_tile(result) == "media-new"


def test_new_tile_wait_uses_full_provider_timeout() -> None:
    worker = object.__new__(GoogleFlowBrowserWorker)
    worker.generation_timeout_seconds = 900
    assert worker._new_tile_timeout_seconds() == 900

    worker.generation_timeout_seconds = 5
    assert worker._new_tile_timeout_seconds() == 30


class _EmptyLocator:
    def count(self):
        return 0

    def nth(self, _index):
        return self

    def is_visible(self):
        return False


class _CompletedTileContainingFailureWords:
    def count(self):
        return 1

    def locator(self, _selector):
        return _EmptyLocator()

    def inner_text(self):
        return "100% previous candidate failed Vision QC; corrective retry requested"

    def get_attribute(self, _name):
        return "completed character tile"


class _NoWaitPage:
    def wait_for_timeout(self, _milliseconds):
        return None


def test_wait_completed_tile_ignores_failure_words_inside_prompt() -> None:
    worker = object.__new__(GoogleFlowBrowserWorker)
    worker.generation_timeout_seconds = 0.2
    tile = _CompletedTileContainingFailureWords()

    result = worker._wait_completed_tile(_NoWaitPage(), tile, kind="image")

    assert result is tile



def test_flow_failure_classifier_stops_unusual_activity_retry() -> None:
    failure = classify_flow_generation_failure(
        "Failed We noticed some unusual activity. You have not been charged."
    )
    assert failure.code == "account_guard"
    assert failure.retryable is False
    assert failure.prompt_repairable is False


def test_flow_failure_classifier_marks_prompt_rejection() -> None:
    failure = classify_flow_generation_failure(
        "Couldn't generate this image because the prompt was rejected by policy."
    )
    assert failure.code == "prompt_rejected"
    assert failure.retryable is False
    assert failure.prompt_repairable is True


def test_new_error_text_ignores_stale_equal_errors_but_detects_new_copy() -> None:
    baseline = ("Failed old generation",)
    current = ("Failed old generation", "Failed old generation")
    assert GoogleFlowBrowserWorker._new_error_text(current, baseline) == "Failed old generation"
    assert GoogleFlowBrowserWorker._new_error_text(baseline, baseline) == ""



def test_account_guard_trips_generation_circuit() -> None:
    worker = object.__new__(GoogleFlowBrowserWorker)
    worker.account_guard_cooldown_seconds = 60
    worker._generation_blocked_until = 0.0
    worker._generation_block_reason = ""
    failure = classify_flow_generation_failure("We noticed some unusual activity")
    worker._trip_generation_circuit(failure)
    assert worker._cooldown_remaining_seconds() > 0
    assert worker._generation_block_reason == "account_guard"
    with pytest.raises(GoogleFlowBrowserError, match="provider_cooldown"):
        worker._assert_generation_allowed()
