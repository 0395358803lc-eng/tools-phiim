import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from flow_story_studio.analysis_providers.xkiro import XKiroClient
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.flow_media import extract_visual_frames, ffmpeg_path
from flow_story_studio.models import AnalyzeRequest, ProductionAcceptance, VideoSettings
from flow_story_studio.providers.base import RenderResult
from flow_story_studio.reference_manager import (
    ReferenceManager,
    promote_accepted_scene_references,
    resolve_scene_reference,
)
from flow_story_studio.render_queue import RenderQueue
from flow_story_studio.storage import ProjectStorage

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
    prompt = "\n".join(scene.flow_prompt for scene in project.scenes)
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


def test_accepted_anchor_promotes_and_resolves_visual_references(tmp_path: Path) -> None:
    project = analyze_story(AnalyzeRequest(name="refs", original_text=SCRIPT))
    scene = project.scenes[0]
    middle = Path("references") / project.id / "qc" / f"{scene.id}-middle.jpg"
    target = tmp_path / middle
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"frame")
    scene.visual_qc.middle_frame = middle.as_posix()
    scene.visual_qc.status = "Passed"
    scene.acceptance = ProductionAcceptance(status="Accepted", score=95)

    assert promote_accepted_scene_references(project, scene)
    relevant_ids = {
        *scene.visual_plan.character_reference_ids,
        scene.visual_plan.location_reference_id,
        *scene.visual_plan.prop_reference_ids,
    }
    refs = [item for item in project.visual_bible.references if item.id in relevant_ids]
    assert refs
    assert all(item.status == "approved" for item in refs)
    assert all(item.approved_reference == middle.as_posix() for item in refs)
    assert resolve_scene_reference(project, scene, tmp_path) == middle.as_posix()


def test_direct_scene_is_blocked_until_predecessor_is_accepted(tmp_path: Path) -> None:
    storage = ProjectStorage(tmp_path / "projects")
    project = analyze_story(AnalyzeRequest(name="dependency", original_text=SCRIPT))
    if len(project.scenes) < 2:
        pytest.skip("Need two production scenes")
    project.settings.provider = "google-flow"
    previous, current = project.scenes[:2]
    current.visual_plan.dependency_mode = "direct"
    current.location_id = previous.location_id
    previous.status = "FailedQC"
    previous.acceptance.status = "Rejected"
    storage.save(project)
    calls: list[str] = []

    class FakeFlow:
        configured = True

        async def generate(self, _project, scene, checkpoint=None) -> RenderResult:
            calls.append(scene.id)
            return RenderResult(job_id="unexpected")

    queue = RenderQueue(storage, FakeFlow())  # type: ignore[arg-type]

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
    reference = project.visual_bible.references[0]

    class FakeFlow:
        configured = True

        async def generate_reference_image(
            self, project_id: str, reference_id: str, prompt: str
        ) -> str:
            assert project_id == project.id
            assert reference_id == reference.id
            assert reference.lock_text in prompt
            relative = Path("references") / project_id / "entities" / f"{reference_id}.png"
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"canonical-reference")
            return relative.as_posix()

    class FakeVision:
        async def inspect_reference(self, current, image_relative: str):
            assert current.id == reference.id
            assert image_relative.endswith(f"{reference.id}.png")
            return 98, []

    manager = ReferenceManager(FakeFlow(), FakeVision(), tmp_path)  # type: ignore[arg-type]
    assert await manager.ensure_reference(project, reference)
    assert reference.status == "approved"
    assert reference.approved_reference
    assert reference.approved_reference in reference.reference_images
