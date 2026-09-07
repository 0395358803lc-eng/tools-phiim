from flow_story_studio.engines.analyzer import analyze_story
from flow_story_studio.film.contracts import stable_hash
from flow_story_studio.film.image_plan import compile_project_image_plans
from flow_story_studio.models import AnalyzeRequest

SCRIPT = """
SCENE 1 — APARTMENT — NIGHT
Khải ngồi bên bàn trong căn hộ, nhìn chiếc vé nằm trước mặt.

SCENE 2 — APARTMENT — NIGHT — CONTINUOUS
Khải đứng dậy và bước về phía cửa, chiếc vé vẫn ở trong tay.
"""


def _project():
    return analyze_story(AnalyzeRequest(name="image plan", original_text=SCRIPT))


def _approve_relevant(project, scene):
    wanted = set(scene.image_plan.character_reference_ids)
    if scene.image_plan.location_reference_id:
        wanted.add(scene.image_plan.location_reference_id)
    wanted.update(scene.image_plan.prop_reference_ids)
    for reference in project.visual_bible.references:
        if reference.id in wanted:
            reference.status = "approved"
            reference.approved_reference = f"references/{reference.id}.png"


def test_image_plan_is_compiled_into_every_scene_and_render_contract() -> None:
    project = _project()

    assert project.scenes
    for scene in project.scenes:
        assert scene.image_plan.plan_hash
        assert scene.image_plan.start_frame_prompt
        assert scene.image_plan.target_frame_prompt
        assert scene.render_contract["image_plan"]["plan_hash"] == scene.image_plan.plan_hash
        assert "generated_start_frame" not in scene.render_contract["image_plan"]
        assert "generated_target_frame" not in scene.render_contract["image_plan"]


def test_canonical_image_plan_blocks_until_required_references_are_approved() -> None:
    project = _project()
    scene = project.scenes[0]

    assert scene.image_plan.start_frame_strategy == "canonical_reanchor"
    if (
        scene.image_plan.character_reference_ids
        or scene.image_plan.location_reference_id
        or scene.image_plan.prop_reference_ids
    ):
        assert scene.image_plan.status == "Blocked"

    _approve_relevant(project, scene)
    compile_project_image_plans(project)

    assert scene.image_plan.status == "Ready"
    assert scene.image_plan.approved_reference_images


def test_direct_scene_inherits_previous_accepted_end_frame() -> None:
    project = _project()
    if len(project.scenes) < 2:
        raise AssertionError("fixture must produce at least two scenes")
    previous, current = project.scenes[:2]
    current.visual_plan.dependency_mode = "direct"
    current.visual_plan.anchor_scene_id = previous.visual_plan.anchor_scene_id or previous.id

    previous.acceptance.status = "Accepted"
    previous.last_frame_file = "frames/scene-001-last.jpg"
    _approve_relevant(project, current)

    compile_project_image_plans(project)

    assert current.image_plan.start_frame_strategy == "previous_accepted_end_frame"
    assert current.image_plan.start_frame_source == "frames/scene-001-last.jpg"
    assert previous.id in current.image_plan.start_frame_prompt
    assert current.image_plan.status == "Ready"


def test_image_plan_hash_ignores_runtime_image_results() -> None:
    project = _project()
    scene = project.scenes[0]
    original = scene.image_plan.plan_hash
    scene.image_plan.generated_start_frame = "runtime/start.png"
    scene.image_plan.generated_target_frame = "runtime/end.png"

    compile_project_image_plans(project)

    assert scene.image_plan.plan_hash == original
    assert scene.image_plan.generated_start_frame == "runtime/start.png"
    assert scene.image_plan.generated_target_frame == "runtime/end.png"

    immutable_payload = scene.render_contract["image_plan"]
    assert stable_hash(immutable_payload) != ""


def test_runtime_reference_approval_does_not_change_semantic_plan_hash() -> None:
    project = _project()
    scene = project.scenes[0]
    original = scene.image_plan.plan_hash

    _approve_relevant(project, scene)
    compile_project_image_plans(project)

    assert scene.image_plan.plan_hash == original
    assert scene.image_plan.status == "Ready"


def test_semantic_plan_change_invalidates_generated_scene_images() -> None:
    project = _project()
    scene = project.scenes[0]
    scene.image_plan.generated_start_frame = "runtime/start.png"
    scene.image_plan.generated_target_frame = "runtime/end.png"
    original = scene.image_plan.plan_hash

    scene.camera = scene.camera + " / tighter angle"
    compile_project_image_plans(project)

    assert scene.image_plan.plan_hash != original
    assert scene.image_plan.generated_start_frame == ""
    assert scene.image_plan.generated_target_frame == ""
