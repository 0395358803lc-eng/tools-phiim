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

    async def vision_json(self, images, prompt, *, model_id=""):
        self.calls.append((list(images), prompt, model_id))
        return self.payload, model_id or "vision-test"


def make_project(tmp_path):
    project = analyze_story(AnalyzeRequest(name="visual qc", original_text=SCRIPT))
    scene = project.scenes[0]
    video = tmp_path / "renders" / "scene.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    scene.result_file = "renders/scene.mp4"

    frame_dir = tmp_path / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "first.jpg",
        "quarter.jpg",
        "middle.jpg",
        "three-quarter.jpg",
        "last.jpg",
    ):
        (frame_dir / name).write_bytes(b"image")
    scene.visual_qc = VisualQCReport(
        first_frame="frames/first.jpg",
        quarter_frame="frames/quarter.jpg",
        middle_frame="frames/middle.jpg",
        three_quarter_frame="frames/three-quarter.jpg",
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
            "action_consistency": 92,
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
    assert len(vision.calls[0][0]) >= 5


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

async def test_scene_visual_qc_rejects_low_component_despite_high_overall_score(tmp_path):
    project, scene = make_project(tmp_path)
    vision = FakeVision(
        {
            "character_identity": 98,
            "location_identity": 98,
            "prop_consistency": 60,
            "wardrobe_consistency": 98,
            "lighting_consistency": 98,
            "action_consistency": 62,
            "composition_consistency": 98,
            "score": 96,
            "issues": [],
        }
    )
    analyzer = VisualQCAnalyzer(tmp_path, vision)

    report = await analyzer.inspect_scene(project, scene)

    assert report.status == "Failed"
    assert report.score == 96
    codes = {issue.code for issue in report.issues}
    assert "VISUAL_PROP_CONSISTENCY_BELOW_THRESHOLD" in codes
    assert "VISUAL_ACTION_CONSISTENCY_BELOW_THRESHOLD" in codes


async def test_continuity_qc_rejects_low_component_despite_high_overall_score(
    tmp_path, monkeypatch
):
    project, scene = make_project(tmp_path)
    monkeypatch.setattr(visual_qc_module, "is_direct_continuation", lambda *_args: True)
    vision = FakeVision(
        {
            "character_match": 98,
            "location_match": 98,
            "wardrobe_match": 98,
            "prop_state_match": 55,
            "lighting_match": 98,
            "screen_direction_match": 98,
            "score": 95,
            "issues": [],
        }
    )
    analyzer = VisualQCAnalyzer(tmp_path, vision)

    report = await analyzer.inspect_continuity(project, scene, scene)

    assert report.status == "Failed"
    assert report.score == 95
    assert any(
        issue.code == "CONTINUITY_PROP_STATE_MATCH_BELOW_THRESHOLD"
        for issue in report.issues
    )



async def test_project_master_qc_uses_downstream_component_floor(tmp_path):
    project, _scene = make_project(tmp_path)
    reference = project.visual_bible.references[0]
    reference.entity_type = "character"
    target = tmp_path / "references" / "master.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"image")
    vision = FakeVision(
        {
            "spec_match": 96,
            "identity_clarity": 94,
            "downstream_reusability": 72,
            "transient_state_control": 93,
            "continuity_safety": 91,
            "score": 95,
            "issues": [],
        }
    )
    analyzer = VisualQCAnalyzer(tmp_path, vision)

    score, issues = await analyzer.inspect_reference_for_project(
        project,
        reference,
        "references/master.jpg",
        model_id="vision-test",
    )

    assert score == 72
    assert any(
        issue.code == "MASTER_DOWNSTREAM_REUSABILITY_BELOW_THRESHOLD"
        and issue.severity == "error"
        for issue in issues
    )
    assert "DOWNSTREAM SCENES THAT WILL REUSE THIS MASTER" in vision.calls[0][1]


async def test_project_master_qc_escalates_character_background_warning(tmp_path):
    project, _scene = make_project(tmp_path)
    reference = project.visual_bible.references[0]
    reference.entity_type = "character"
    target = tmp_path / "references" / "master.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"image")
    vision = FakeVision(
        {
            "spec_match": 95,
            "identity_clarity": 95,
            "downstream_reusability": 95,
            "transient_state_control": 95,
            "continuity_safety": 95,
            "score": 95,
            "issues": [
                {
                    "code": "background_contamination",
                    "severity": "warning",
                    "message": "A story-specific alley is visible.",
                }
            ],
        }
    )
    analyzer = VisualQCAnalyzer(tmp_path, vision)

    score, issues = await analyzer.inspect_reference_for_project(
        project,
        reference,
        "references/master.jpg",
        model_id="vision-test",
    )

    assert score == 95
    issue = next(item for item in issues if item.code == "background_contamination")
    assert issue.severity == "error"
