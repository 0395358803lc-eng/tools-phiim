from types import SimpleNamespace

import flow_story_studio.visual_qc as visual_qc_module
from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.models import AnalyzeRequest, VisualQCReport
from flow_story_studio.visual_qc import VisualQCAnalyzer


SCRIPT = """
SCENE 1 — ROOM — NIGHT
A person crosses the room and stops beside the table.
"""


class FakeVision:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def vision_json(self, images, prompt):
        self.calls.append((list(images), prompt))
        return self.payload, "vision-test"


def make_project(tmp_path):
    project = analyze_story(AnalyzeRequest(name="visual qc", original_text=SCRIPT))
    scene = project.scenes[0]
    video = tmp_path / "renders" / "scene.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    scene.result_file = "renders/scene.mp4"

    frame_dir = tmp_path / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    for name in ("first.jpg", "middle.jpg", "last.jpg"):
        (frame_dir / name).write_bytes(b"image")
    scene.visual_qc = VisualQCReport(
        first_frame="frames/first.jpg",
        middle_frame="frames/middle.jpg",
        last_frame="frames/last.jpg",
    )
    return project, scene


async def test_scene_visual_qc_passes_with_observed_frames(tmp_path):
    project, scene = make_project(tmp_path)
    vision = FakeVision(
        {
            "character_identity": 94,
            "location_identity": 93,
            "prop_consistency": 92,
            "wardrobe_consistency": 95,
            "lighting_consistency": 91,
            "composition_consistency": 90,
            "score": 93,
            "issues": [],
        }
    )
    analyzer = VisualQCAnalyzer(tmp_path, vision)

    report = await analyzer.inspect_scene(project, scene)

    assert report.status == "Passed"
    assert report.score == 93
    assert report.model_id == "vision-test"
    assert len(vision.calls) == 1
    assert len(vision.calls[0][0]) >= 3


async def test_scene_visual_qc_fails_closed_when_video_is_missing(tmp_path):
    project, scene = make_project(tmp_path)
    scene.result_file = "renders/missing.mp4"
    analyzer = VisualQCAnalyzer(tmp_path, FakeVision({}))

    report = await analyzer.inspect_scene(project, scene)

    assert report.status == "Unavailable"
    assert report.issues[0].code == "VIDEO_MISSING"


async def test_reference_qc_parses_score_and_issues(tmp_path):
    target = tmp_path / "references" / "phone.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"image")
    reference = SimpleNamespace(
        entity_type="prop",
        name="Phone",
        lock_text="A black rectangular phone with stable shape and material.",
    )
    vision = FakeVision(
        {
            "score": 96,
            "issues": [
                {"code": "MINOR_NOTE", "severity": "warning", "message": "Small reflection"}
            ],
        }
    )
    analyzer = VisualQCAnalyzer(tmp_path, vision)

    score, issues = await analyzer.inspect_reference(reference, "references/phone.png")

    assert score == 96
    assert issues[0].code == "MINOR_NOTE"
    assert issues[0].severity == "warning"


async def test_continuity_qc_passes_for_direct_boundary(tmp_path, monkeypatch):
    project, scene = make_project(tmp_path)
    monkeypatch.setattr(visual_qc_module, "is_direct_continuation", lambda *_args: True)
    vision = FakeVision(
        {
            "character_match": 93,
            "location_match": 92,
            "wardrobe_match": 94,
            "prop_state_match": 91,
            "lighting_match": 90,
            "screen_direction_match": 95,
            "score": 93,
            "issues": [],
        }
    )
    analyzer = VisualQCAnalyzer(tmp_path, vision)

    report = await analyzer.inspect_continuity(project, scene, scene)

    assert report.status == "Passed"
    assert report.score == 93
    assert report.model_id == "vision-test"


async def test_continuity_qc_is_not_applicable_without_direct_link(tmp_path, monkeypatch):
    project, scene = make_project(tmp_path)
    monkeypatch.setattr(visual_qc_module, "is_direct_continuation", lambda *_args: False)
    analyzer = VisualQCAnalyzer(tmp_path, FakeVision({}))

    report = await analyzer.inspect_continuity(project, scene, scene)

    assert report.status == "NotApplicable"
    assert report.score == 100
