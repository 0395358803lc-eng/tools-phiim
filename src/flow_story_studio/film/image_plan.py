from __future__ import annotations

from ..engines.prompt_generator import NEGATIVE_CONSTRAINTS, compact_state
from ..models import Project, Scene, SceneImagePlan, VisualReference
from .contracts import stable_hash


def _reference_map(project: Project) -> dict[str, VisualReference]:
    return {item.id: item for item in project.visual_bible.references}


def _reference_summary(reference: VisualReference) -> str:
    return (
        f"{reference.id} [{reference.entity_type}] {reference.name}: "
        f"LOCKED SPECIFICATION: {reference.lock_text}"
    )


def _scene_reference_ids(scene: Scene) -> list[str]:
    values = list(scene.visual_plan.character_reference_ids)
    if scene.visual_plan.location_reference_id:
        values.append(scene.visual_plan.location_reference_id)
    values.extend(scene.visual_plan.prop_reference_ids)
    return list(dict.fromkeys(values))


def _best_previous_end_frame(previous: Scene | None) -> str:
    if previous is None:
        return ""
    if previous.acceptance.status != "Accepted":
        return ""
    return (
        previous.last_frame_file
        or previous.visual_qc.last_frame
        or previous.image_plan.generated_target_frame
    )


def compile_scene_image_plan(
    project: Project,
    scene: Scene,
    previous: Scene | None,
) -> SceneImagePlan:
    refs = _reference_map(project)
    ref_ids = _scene_reference_ids(scene)
    relevant = [refs[item_id] for item_id in ref_ids if item_id in refs]
    ref_status = {item.id: item.status for item in relevant}
    approved_images = list(
        dict.fromkeys(
            item.approved_reference
            for item in relevant
            if item.status == "approved" and item.approved_reference
        )
    )
    missing_reference_ids = [
        item.id
        for item in relevant
        if item.status != "approved" or not item.approved_reference
    ]

    direct = scene.visual_plan.dependency_mode == "direct" and previous is not None
    previous_frame = _best_previous_end_frame(previous)
    if direct:
        start_strategy = "previous_accepted_end_frame"
        start_requirement = (
            f"Use the accepted FINAL frame of {previous.id} as the exact physical START frame. "
            "Also reuse the approved Project Master References as identity/world guards. "
            "Do not independently regenerate character identity, wardrobe, location layout, "
            "prop identity, lighting direction or screen direction at this boundary."
        )
        start_prompt = (
            f"SCENE {scene.id} START FRAME IS INHERITED, NOT INDEPENDENTLY GENERATED.\n"
            f"Required source: accepted final frame of {previous.id}.\n"
            "Runtime binding: resolve the accepted frame file immediately before generation.\n"
            "MASTER REUSE RULE: the approved Character/Location/Prop Masters remain mandatory "
            "identity guards; never create a new entity identity inside this scene.\n"
            f"ENTRY STATE:\n{compact_state(scene.start_state)}\n"
            "Preserve the inherited pixels/physical state as the continuity anchor."
        )
        status = "Ready" if previous_frame and not missing_reference_ids else "Blocked"
    else:
        start_strategy = "canonical_reanchor"
        start_requirement = (
            "Create a new scene composition by REUSING the project's already-approved Character, "
            "Location and Prop Master References. Do not create or redesign any entity identity "
            "inside this scene. Do not copy the previous scene composition unless the source "
            "explicitly requires it."
        )
        references_text = "\n".join(_reference_summary(item) for item in relevant)
        start_prompt = f"""SCENE {scene.id} — CANONICAL START FRAME

SOURCE TRUTH:
{scene.source_text}

PROJECT MASTER REFERENCES — REUSE ONLY:
{references_text or "No entity Master Reference is required by this scene."}

MASTER REUSE RULE:
These are Project-level Masters. Reuse their identity/world appearance exactly. This scene may
change pose, framing and source-authorized state only; it must never generate a replacement
Character, Location or Prop identity.

IDENTITY / WORLD LOCK:
{scene.visual_plan.lock_prompt or "Preserve the canonical project visual identity."}

ENTRY STATE:
{compact_state(scene.start_state)}

SHOT:
Camera: {scene.camera}
Lighting: {scene.lighting}
Atmosphere: {scene.atmosphere}
Visual style: {project.visual_style}
Aspect ratio: {project.settings.aspect_ratio}

Create the first production frame for this scene. Preserve exact recurring face geometry, age,
body proportions, hairstyle, wardrobe, accessories, architecture, room layout, prop form/color/
condition, palette and motivated light sources. The image must represent the scene ENTRY state,
before completing actions that belong later in this scene."""
        status = "Blocked" if missing_reference_ids else "Ready"

    target_prompt = f"""SCENE {scene.id} — TARGET / EXIT KEYFRAME

SOURCE TRUTH:
{scene.source_text}

IDENTITY / WORLD LOCK:
{scene.visual_plan.lock_prompt or "Preserve the canonical project visual identity."}

REQUIRED ACTION:
{scene.action}

TARGET EXIT STATE:
{compact_state(scene.end_state)}

SHOT:
Camera: {scene.camera}
Lighting: {scene.lighting}
Atmosphere: {scene.atmosphere}
Visual style: {project.visual_style}
Aspect ratio: {project.settings.aspect_ratio}

Create a target keyframe that can serve as the visual destination of this scene. REUSE the exact
same Project Master identities: same people, same faces, same wardrobe, same location geometry and
same prop identity as the approved Masters/start anchor. Never generate a replacement entity
identity. Only source-authorized state changes may appear. Do not replay completed actions from
earlier scenes and do not introduce future-scene events."""

    identity_lock = (
        "Project Master References are authoritative and reusable, never scene-local. "
        "Recurring character identity, facial geometry, age, build, hair, skin tone, "
        "wardrobe and accessories are immutable unless the screenplay explicitly changes "
        "them. Location architecture, spatial anchors, fixed objects, palette and prop "
        "identity are immutable. A scene may create a new composition, but it may never "
        "create a replacement Character/Location/Prop identity."
    )
    composition_lock = (
        f"Camera={scene.camera}; lighting={scene.lighting}; atmosphere={scene.atmosphere}; "
        f"dependency={scene.visual_plan.dependency_mode}; anchor="
        f"{scene.visual_plan.anchor_scene_id or scene.id}."
    )

    plan_payload: dict[str, object] = {
        "scene_id": scene.id,
        "dependency_mode": scene.visual_plan.dependency_mode,
        "anchor_scene_id": scene.visual_plan.anchor_scene_id,
        "start_frame_strategy": start_strategy,
        "start_frame_requirement": start_requirement,
        "character_reference_ids": list(scene.visual_plan.character_reference_ids),
        "location_reference_id": scene.visual_plan.location_reference_id,
        "prop_reference_ids": list(scene.visual_plan.prop_reference_ids),
        "identity_lock": identity_lock,
        "composition_lock": composition_lock,
        "start_frame_prompt": start_prompt,
        "target_frame_prompt": target_prompt,
        "negative_prompt": NEGATIVE_CONSTRAINTS,
    }

    plan_hash = stable_hash(plan_payload)
    current = scene.image_plan
    same_plan = current.plan_hash == plan_hash
    return SceneImagePlan(
        status=(
            current.status
            if same_plan and current.status in {"Generated", "Approved", "Rejected"}
            else status
        ),
        renderer_status=current.renderer_status,
        dependency_mode=scene.visual_plan.dependency_mode,
        anchor_scene_id=scene.visual_plan.anchor_scene_id or scene.id,
        start_frame_strategy=start_strategy,
        start_frame_source=previous_frame if direct else scene.reference_image,
        start_frame_requirement=start_requirement,
        target_frame_strategy="generate_scene_exit_keyframe",
        character_reference_ids=list(scene.visual_plan.character_reference_ids),
        location_reference_id=scene.visual_plan.location_reference_id,
        prop_reference_ids=list(scene.visual_plan.prop_reference_ids),
        reference_status=ref_status,
        approved_reference_images=approved_images,
        identity_lock=identity_lock,
        composition_lock=composition_lock,
        start_frame_prompt=start_prompt,
        target_frame_prompt=target_prompt,
        negative_prompt=NEGATIVE_CONSTRAINTS,
        plan_hash=plan_hash,
        generated_start_frame=current.generated_start_frame if same_plan else "",
        generated_target_frame=current.generated_target_frame if same_plan else "",
    )


def compile_project_image_plans(project: Project) -> Project:
    previous: Scene | None = None
    for scene in project.scenes:
        scene.image_plan = compile_scene_image_plan(project, scene, previous)
        previous = scene
    return project
