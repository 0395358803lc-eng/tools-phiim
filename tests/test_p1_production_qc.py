import asyncio
import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest

from flow_story_studio.analysis_providers.xkiro import XKiroClient
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.film.state_delta import continuity_state_hash
from flow_story_studio.media_tools import extract_visual_frames, ffmpeg_path
from flow_story_studio.models import AnalyzeRequest, ProductionAcceptance, VideoSettings
from flow_story_studio.providers.base import RenderResult
from flow_story_studio.providers.registry import ProviderRegistry
from flow_story_studio.reference_manager import (
    ReferenceManager,
    _generation_prompt,
    resolve_scene_reference,
    scene_outputs_cannot_promote_master_references,
)
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.storage import ProjectStorage
from flow_story_studio.visual_bible import canonical_reference_lock

MOJIBAKE_PATTERNS = (
    r"KhÃ´ng",
    r"tÃ¬m",
    r"tháº¥y",
    r"há»£p",
    r"lá»‡",
    r"khá»›p",
    r"GiÃ¡",
    r"Â»",
)

SCRIPT = """
TARGET RUNTIME: 16 seconds

CHARACTERS
- ALEX, adult man in a dark coat.
- MAYA, adult woman.

PROPS
- Blue paper ticket.

SCENE 1 — STATION — NIGHT
ALEX
I found the ticket.

MAYA (THROUGH THE PHONE)
Do not leave.

Alex holds the blue paper ticket beside the bench.

Alex walks two steps forward while holding the same ticket.
"""


def test_offline_analyze_injects_source_grounded_audio_lock() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="audio lock",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )
    prompt = "\n".join(scene.render_prompt for scene in project.scenes)
    assert "AUDIO / DIALOGUE LOCK:" in prompt
    assert 'ALEX: "I found the ticket."' in prompt
    assert 'MAYA: "Do not leave."' in prompt
    assert "Do not paraphrase" in prompt


@pytest.mark.asyncio
async def test_xkiro_vision_json_sends_multimodal_payload(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/usage":
            return httpx.Response(200, json={"free_tokens_remaining": 1000})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "minimax/minimax-m3:free",
                            "display_name": "MiniMax M3",
                            "owned_by": "minimax",
                            "access_tier": "free",
                            "capabilities": {"vision": True},
                        }
                    ]
                },
            )
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            seen["body"] = body
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "score": 97,
                                        "issues": [],
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    image = tmp_path / "frame.png"
    image.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6360f8cfc000000301010018dd8db10000000049454e44ae426082"
        )
    )
    client = XKiroClient(transport=httpx.MockTransport(handler))
    await client.connect("sk-vision-test")
    result, model = await client.vision_json([image], "Return JSON")
    assert model == "minimax/minimax-m3:free"
    assert result["score"] == 97
    body = seen["body"]
    assert isinstance(body, dict)
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[-1] == {"type": "text", "text": "Return JSON"}


@pytest.mark.skipif(ffmpeg_path() is None, reason="FFmpeg is not installed")
def test_frame_extractor_creates_first_middle_last(tmp_path: Path) -> None:
    ffmpeg = ffmpeg_path()
    assert ffmpeg
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=1:r=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    frames = asyncio.run(extract_visual_frames(tmp_path, "project", "scene", video))
    assert frames.first and frames.middle and frames.last
    for relative in (frames.first, frames.middle, frames.last):
        assert (tmp_path / relative).is_file()


def test_no_vietnamese_mojibake_in_source() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    source_files = sorted(root.rglob("*.py"))
    assert source_files
    regexes = [re.compile(pattern, re.IGNORECASE) for pattern in MOJIBAKE_PATTERNS]
    offenders: list[str] = []
    for path in source_files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(rx.search(line) for rx in regexes):
                offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_accepted_scene_cannot_promote_output_frame_to_master_reference(
    tmp_path: Path,
) -> None:
    project = analyze_story(AnalyzeRequest(name="refs", original_text=SCRIPT))
    scene = project.scenes[0]
    middle = Path("references") / project.id / "qc" / f"{scene.id}-middle.jpg"
    target = tmp_path / middle
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"frame")
    scene.visual_qc.middle_frame = middle.as_posix()
    scene.visual_qc.status = "Passed"
    scene.acceptance = ProductionAcceptance(status="Accepted", score=95)

    assert not scene_outputs_cannot_promote_master_references(project, scene)
    relevant_ids = {
        *scene.visual_plan.character_reference_ids,
        scene.visual_plan.location_reference_id,
        *scene.visual_plan.prop_reference_ids,
    }
    refs = [item for item in project.visual_bible.references if item.id in relevant_ids]
    assert refs
    assert all(item.status != "approved" for item in refs)
    assert all(not item.approved_reference for item in refs)
    assert resolve_scene_reference(project, scene, tmp_path) == ""


@pytest.mark.asyncio
async def test_mock_post_render_qc_stamps_render_settings_identity(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = analyze_story(
        AnalyzeRequest(
            name="render identity",
            original_text=SCRIPT,
            settings=VideoSettings(provider="mock"),
        )
    )
    scene = project.scenes[0]

    queue = RenderQueue(storage)
    await queue._post_render_qc(project, scene)

    assert scene.acceptance.status == "Accepted"
    assert scene.render_provider == project.settings.provider
    assert scene.render_model == project.settings.video_model


def test_direct_scene_is_blocked_until_predecessor_is_accepted(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = analyze_story(AnalyzeRequest(name="dependency", original_text=SCRIPT))
    if len(project.scenes) < 2:
        pytest.skip("Need two production scenes")
    project.settings.provider = "test-renderer"
    previous, current = project.scenes[:2]
    current.visual_plan.dependency_mode = "direct"
    current.location_id = previous.location_id
    current.image_plan.status = "Ready"
    previous.status = "FailedQC"
    previous.acceptance.status = "Rejected"
    storage.save(project)
    calls: list[str] = []

    class FakeRenderer:
        configured = True

        async def generate(self, _project, scene) -> RenderResult:
            calls.append(scene.id)
            return RenderResult(job_id="unexpected")

    registry = ProviderRegistry()
    registry.register("test-renderer", FakeRenderer())
    queue = RenderQueue(storage, provider_registry=registry)

    async def run() -> None:
        await queue.enqueue(project.id, [current.id])
        await queue._queues[project.id].join()
        await queue.shutdown()

    asyncio.run(run())
    latest = storage.get(project.id)
    assert latest is not None
    assert latest.scenes[1].status == "Blocked"
    assert latest.scenes[1].acceptance.status == "Blocked"
    assert calls == []


@pytest.mark.asyncio
async def test_reference_manager_generates_vision_qcs_and_approves(tmp_path: Path) -> None:
    project = analyze_story(AnalyzeRequest(name="reference generation", original_text=SCRIPT))
    project.settings.vision_model = "vision-selected"
    reference = project.visual_bible.references[0]

    class FakeRenderer:
        configured = True

        async def generate_reference_image(
            self,
            current_project,
            reference_id: str,
            prompt: str,
            *,
            ingredient_files=None,
        ) -> str:
            assert current_project is project
            assert reference_id == reference.id
            assert reference.lock_text in prompt
            assert "STRICT FULL-BODY FRAMING" in prompt
            assert "head-to-toe" in prompt
            relative = Path("references") / current_project.id / "entities" / f"{reference_id}.png"
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"canonical-reference")
            return relative.as_posix()

    class FakeVision:
        async def inspect_reference(self, current, image_relative: str, *, model_id=""):
            assert current.id == reference.id
            assert image_relative.endswith(f"{reference.id}.png")
            assert model_id == "vision-selected"
            return 98, []

    manager = ReferenceManager(FakeRenderer(), FakeVision(), tmp_path)  # type: ignore[arg-type]
    assert await manager.ensure_reference(project, reference)
    assert reference.status == "approved"
    assert reference.approved_reference
    assert reference.approved_reference in reference.reference_images


def test_direct_dependency_blocks_stale_start_state_hash(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = analyze_story(AnalyzeRequest(name="accepted-state-hash", original_text=SCRIPT))
    if len(project.scenes) < 2:
        pytest.skip("Need two production scenes")
    previous, current = project.scenes[:2]
    current.visual_plan.dependency_mode = "direct"
    current.location_id = previous.location_id
    current.start_state = previous.end_state.model_copy(deep=True)

    previous.accepted_end_state = previous.end_state.model_copy(deep=True)
    previous.accepted_state_hash = continuity_state_hash(previous.accepted_end_state)
    current.start_state.notes = "stale mutation after accepted state commit"

    queue = RenderQueue(storage)
    queue._is_finally_accepted = lambda _project, _scene: True  # type: ignore[method-assign]

    reason = queue._dependency_block_reason(project, current)
    assert "không khớp accepted state" in reason


def test_location_master_prompt_uses_structural_clues_not_raw_story_text() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="location structural prompt",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )
    reference = next(
        item for item in project.visual_bible.references if item.entity_type == "location"
    )
    location = next(item for item in project.locations if item.id == reference.entity_id)
    location.architecture = "Kiến trúc đặc trưng của địa điểm, giữ nguyên tuyệt đối"
    location.space = "Bố cục địa điểm được thiết lập ở cảnh đầu và tái sử dụng"
    location.interior = "Nội thất và vật liệu nhất quán của địa điểm"
    location.objects = []
    location.spatial_anchors = "Giữ nguyên vị trí tương đối của vật thể quan trọng"

    prompt = _generation_prompt(project, reference)

    assert "canonical LOCATION MASTER inspection plate" in prompt
    assert "SOURCE-GROUNDED STRUCTURAL CLUES ONLY" in prompt
    assert "Alex holds the blue paper ticket beside the bench." not in prompt
    assert "MAYA (THROUGH THE PHONE)" not in prompt
    assert "LIGHTING MUST BE NEUTRAL INSPECTION EXPOSURE" in prompt
    assert "STYLE REFERENCE FOR MATERIAL REALISM ONLY" in prompt


def test_location_corrective_prompt_deduplicates_root_issue_codes() -> None:
    from flow_story_studio.models import VisualIssue

    project = analyze_story(
        AnalyzeRequest(
            name="location corrective prompt",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )
    reference = next(
        item for item in project.visual_bible.references if item.entity_type == "location"
    )
    reference.status = "rejected"
    reference.vision_issues = [
        VisualIssue(
            code="transient_state_control",
            severity="warning",
            message="Temporary light state present.",
        ),
        VisualIssue(
            code="transient_state_control",
            severity="warning",
            message="Duplicate evaluator finding.",
        ),
        VisualIssue(
            code="MASTER_TRANSIENT_STATE_CONTROL_BELOW_THRESHOLD",
            severity="error",
            message="Aggregate score failure.",
        ),
    ]

    prompt = _generation_prompt(project, reference)

    assert prompt.count("- transient_state_control:") == 1
    assert "MASTER_TRANSIENT_STATE_CONTROL_BELOW_THRESHOLD" not in prompt


def test_location_generation_and_vision_share_canonical_structural_clues() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="location canonical alignment",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )
    reference = next(
        item for item in project.visual_bible.references if item.entity_type == "location"
    )
    location = next(item for item in project.locations if item.id == reference.entity_id)
    location.architecture = "Kiến trúc đặc trưng của địa điểm, giữ nguyên tuyệt đối"
    location.space = "Bố cục địa điểm được thiết lập ở cảnh đầu và tái sử dụng"
    location.interior = "Nội thất và vật liệu nhất quán của địa điểm"
    location.objects = []
    location.spatial_anchors = "Giữ nguyên vị trí tương đối của vật thể quan trọng"

    lock = canonical_reference_lock(project, reference)
    prompt = _generation_prompt(project, reference)

    assert "SOURCE-GROUNDED FIXED TOPOLOGY" in lock
    assert "waiting benches / seating zone" in lock
    assert "SOURCE-GROUNDED STRUCTURAL CLUES ONLY" in prompt
    assert "waiting benches / seating zone" in prompt
    assert "Alex holds the blue paper ticket beside the bench." not in prompt
    assert "Alex holds the blue paper ticket beside the bench." not in lock


def test_location_master_extracts_fixed_locker_bank_anchor() -> None:
    script = """
TARGET RUNTIME: 8 seconds

CẢNH 1 — SẢNH NHÀ GA CŨ — ĐÊM
Minh đi về phía dãy tủ gửi đồ. Anh dừng trước tủ số 17 rồi tra chìa khóa vào ổ.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="locker topology",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    location = next(item for item in project.locations if "SẢNH NHÀ GA" in item.name.upper())
    reference = next(
        item
        for item in project.visual_bible.references
        if item.entity_type == "location" and item.entity_id == location.id
    )

    lock = canonical_reference_lock(project, reference)
    prompt = _generation_prompt(project, reference)

    assert "fixed luggage-locker bank" in lock
    assert "fixed luggage-locker bank" in prompt
    assert "tủ số 17" not in prompt
    assert "tra chìa khóa" not in prompt


def test_location_master_prompt_neutralizes_window_exterior_state() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="window neutralization",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )
    reference = next(
        item for item in project.visual_bible.references if item.entity_type == "location"
    )

    prompt = _generation_prompt(project, reference)

    assert "exterior beyond them must be soft neutral/featureless" in prompt
    assert "must not encode cityscape brightness, sky color, weather" in prompt


def test_location_canonical_lock_replaces_stale_generic_anchor_duplicates() -> None:
    script = """
TARGET RUNTIME: 8 seconds

CẢNH 1 — SẢNH NHÀ GA CŨ — ĐÊM
Minh đặt chiếc vé lên quầy vé rồi đi về phía dãy tủ gửi đồ.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="location anchor dedupe",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    location = next(item for item in project.locations if "SẢNH NHÀ GA" in item.name.upper())
    location.spatial_anchors = (
        "SOURCE-GROUNDED FIXED TOPOLOGY: fixed ticket counter; fixed counter. "
        "Preserve these anchors and their relative geometry across every Master/scene"
    )
    reference = next(
        item
        for item in project.visual_bible.references
        if item.entity_type == "location" and item.entity_id == location.id
    )

    lock = canonical_reference_lock(project, reference)

    assert "fixed ticket counter" in lock
    assert "fixed luggage-locker bank" in lock
    assert "; fixed counter" not in lock


def test_location_master_prompt_bans_rolling_stock_and_temporary_vehicles() -> None:
    project = analyze_story(
        AnalyzeRequest(
            name="location rolling stock exclusion",
            original_text=SCRIPT,
            settings=VideoSettings(scene_duration=8),
        )
    )
    reference = next(
        item for item in project.visual_bible.references if item.entity_type == "location"
    )

    prompt = _generation_prompt(project, reference)

    assert "ABSOLUTELY NO people, trains, vehicles, carts, trolleys" in prompt


def test_location_master_entry_threshold_and_furniture_are_unambiguous() -> None:
    script = """
TARGET RUNTIME: 8 seconds

CẢNH 1 — CĂN HỘ CỦA MINH — ĐÊM
Một chiếc vé trượt qua khe dưới cửa. Minh nhặt nó lên cạnh bàn.
"""
    project = analyze_story(
        AnalyzeRequest(
            name="apartment threshold topology",
            original_text=script,
            settings=VideoSettings(scene_duration=8),
        )
    )
    reference = next(
        item for item in project.visual_bible.references if item.entity_type == "location"
    )

    lock = canonical_reference_lock(project, reference)
    prompt = _generation_prompt(project, reference)

    assert "main entry door / threshold" in lock
    assert "under-door clearance" not in lock
    assert "single stable table / work surface fixed relative to wall-door-window axes" in lock
    assert "MUST be OFF/unlit in the Master" in prompt
    assert "omit unsourced benches" in prompt
    assert "stools, chairs and decorative furniture" in prompt
