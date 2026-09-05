from pathlib import Path

import pytest

from flow_story_studio import visual_qc
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VisualQCReport

SCRIPT = """
SCENE 1 — ROOM — NIGHT
ALEX crosses the room and stops beside the table.
"""


class FakeVision:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def vision_json(self, images, prompt):
        self.calls.append((images, prompt))
        return self.payload, "vision-test-model"


def _project():
    return analyze_story(AnalyzeRequest(name="visual qc", original_text=SCRIPT))


def test_visual_qc_helpers_are_bounded_and_path_safe(tmp_path: Path):
    assert visual_qc._bounded("101.4") == 100
    assert visual_qc._bounded("-2") == 0
    assert visual_qc._bounded("bad", 7) == 7

    parsed = visual_qc._issues(
        [
            "note",
            {"code": "WARN", "severity": "warning", "message": "warning"},
            {"code": "ERR", "severity": "error", "detail": "error"},
            123,
        ]
    )
    assert [item.code for item in parsed] == ["VISION_NOTE", "WARN", "ERR"]
    assert [item.severity for item in parsed] == ["warning", "warning", "error"]

    inside = tmp_path / "frames" / "one.jpg"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"frame")
    assert visual_qc._data_path(tmp_path, "frames/one.jpg") == inside.resolve()
    assert visual_qc._data_path(tmp_path, "../outside.jpg") is None
    assert visual_qc._data_path(tmp_path, "") is None


@pytest.mark.asyncio
async def test_inspect_scene_fails_closed_when_video_is_missing(tmp_path: Path):
    project = _project()
    scene = project.scenes[0]
    analyzer = visual_qc.VisualQCAnalyzer(tmp_path, FakeVision({}))  # type: ignore[arg-type]

    report = await analyzer.inspect_scene(project, scene)

    assert report.status == "Unavailable"
    assert report.issues[0].code == "VIDEO_MISSING"


@pytest.mark.asyncio
async def test_inspect_scene_scores_real_frame_files_without_ffmpeg(tmp_path: Path):
    project = _project()
    scene = project.scenes[0]

    video = tmp_path / "renders" / project.id / scene.id / "scene.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    scene.result_file = video.relative_to(tmp_path).as_posix()

    frame_paths = []
    for name in ("first.jpg", "middle.jpg", "last.jpg"):
        frame = tmp_path / "frames" / name
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(name.encode())
        frame_paths.append(frame.relative_to(tmp_path).as_posix())
    scene.visual_qc = VisualQCReport(
        first_frame=frame_paths[0],
        middle_frame=frame_paths[1],
        last_frame=frame_paths[2],
    )

    vision = FakeVision(
        {
            "character_identity": 96,
            "location_identity": 95,
            "prop_consistency": 94,
            "wardrobe_consistency": 93,
            "lighting_consistency": 92,
            "action_consistency": 91,
            "composition_consistency": 91,
            "score": 94,
            "issues": [{"code": "MINOR", "severity": "warning", "message": "minor"}],
        }
    )
    analyzer = visual_qc.VisualQCAnalyzer(tmp_path, vision)  # type: ignore[arg-type]

    report = await analyzer.inspect_scene(project, scene)

    assert report.status == "Passed"
    assert report.score == 94
    assert report.model_id == "vision-test-model"
    assert len(vision.calls) == 1
    assert len(vision.calls[0][0]) == 3


@pytest.mark.asyncio
async def test_inspect_reference_handles_missing_and_approved_candidate(tmp_path: Path):
    project = _project()
    reference = project.visual_bible.references[0]
    vision = FakeVision({"score": 93, "issues": []})
    analyzer = visual_qc.VisualQCAnalyzer(tmp_path, vision)  # type: ignore[arg-type]

    missing_score, missing_issues = await analyzer.inspect_reference(reference, "missing.png")
    assert missing_score == 0
    assert missing_issues[0].code == "REFERENCE_MISSING"

    image = tmp_path / "references" / "candidate.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    score, issues = await analyzer.inspect_reference(
        reference,
        image.relative_to(tmp_path).as_posix(),
    )
    assert score == 93
    assert issues == []
    assert len(vision.calls) == 1


@pytest.mark.asyncio
async def test_continuity_qc_is_not_applicable_without_direct_predecessor(tmp_path: Path):
    project = _project()
    analyzer = visual_qc.VisualQCAnalyzer(tmp_path, FakeVision({}))  # type: ignore[arg-type]

    report = await analyzer.inspect_continuity(project, None, project.scenes[0])

    assert report.status == "NotApplicable"
    assert report.score == 100


@pytest.mark.asyncio
async def test_continuity_qc_compares_boundary_frames(tmp_path: Path, monkeypatch):
    project = _project()
    previous = project.scenes[0]
    current = previous.model_copy(deep=True)
    current.id = "SC-002"
    current.order = 2
    project.scenes = [previous, current]

    previous_last = tmp_path / "frames" / "previous-last.jpg"
    current_first = tmp_path / "frames" / "current-first.jpg"
    previous_last.parent.mkdir(parents=True)
    previous_last.write_bytes(b"previous")
    current_first.write_bytes(b"current")
    previous.visual_qc.last_frame = previous_last.relative_to(tmp_path).as_posix()
    current.visual_qc.first_frame = current_first.relative_to(tmp_path).as_posix()

    monkeypatch.setattr(
        "flow_story_studio.visual_qc.is_direct_continuation",
        lambda *_args: True,
    )
    vision = FakeVision(
        {
            "character_match": 96,
            "location_match": 95,
            "wardrobe_match": 94,
            "prop_state_match": 93,
            "lighting_match": 92,
            "screen_direction_match": 91,
            "score": 94,
            "issues": [],
        }
    )
    analyzer = visual_qc.VisualQCAnalyzer(tmp_path, vision)  # type: ignore[arg-type]

    report = await analyzer.inspect_continuity(project, previous, current)

    assert report.status == "Passed"
    assert report.score == 94
    assert report.model_id == "vision-test-model"
    assert len(vision.calls[0][0]) == 2
