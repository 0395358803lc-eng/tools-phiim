"""Single fail-closed production acceptance predicate.

All downstream consumers must use this module instead of trusting mutable status flags.
"""

from __future__ import annotations

from .models import Project, Scene
from .scene_contracts import verify_scene_contract


def _below_threshold(values: dict[str, int], threshold: int) -> list[str]:
    return [f"{name}={score}<{threshold}" for name, score in values.items() if score < threshold]


def scene_production_blockers(
    project: Project,
    scene: Scene,
    *,
    require_result: bool = True,
) -> list[str]:
    """Return every reason a scene is not safe for dependency chaining/final merge."""
    threshold = project.settings.quality_threshold
    reasons: list[str] = []

    if scene.status != "Accepted":
        reasons.append(f"scene status is {scene.status}")
    if scene.acceptance.status != "Accepted":
        reasons.append(f"production acceptance is {scene.acceptance.status}")
    if scene.acceptance.score < threshold:
        reasons.append(
            f"production acceptance score {scene.acceptance.score} is below {threshold}"
        )
    if not scene.ai_locked:
        reasons.append("scene is not AI continuity locked")
    if not verify_scene_contract(scene):
        reasons.append("scene packet contract hash is missing or stale")
    if require_result and not scene.result_file:
        reasons.append("rendered video file is missing")

    if scene.quality is None:
        reasons.append("preflight quality report is missing")
    elif scene.quality.score < threshold:
        reasons.append(f"preflight quality score {scene.quality.score} is below {threshold}")

    if scene.visual_qc.status != "Passed":
        reasons.append(f"visual QC is {scene.visual_qc.status}")
    visual_components = {
        "character_identity": scene.visual_qc.character_identity,
        "location_identity": scene.visual_qc.location_identity,
        "prop_consistency": scene.visual_qc.prop_consistency,
        "wardrobe_consistency": scene.visual_qc.wardrobe_consistency,
        "lighting_consistency": scene.visual_qc.lighting_consistency,
        "action_consistency": scene.visual_qc.action_consistency,
        "composition_consistency": scene.visual_qc.composition_consistency,
    }
    visual_low = _below_threshold(visual_components, threshold)
    if visual_low:
        reasons.append("visual component below threshold: " + ", ".join(visual_low))

    if project.settings.provider == "google-flow":
        if not all(
            (
                scene.visual_qc.first_frame,
                scene.visual_qc.middle_frame,
                scene.visual_qc.last_frame,
            )
        ):
            reasons.append("visual QC boundary evidence is incomplete")
        if not scene.visual_qc.model_id:
            reasons.append("visual QC model evidence is missing")

    continuity = scene.continuity_qc
    if scene.visual_plan.dependency_mode == "direct":
        if continuity.status != "Passed":
            reasons.append(f"direct continuity QC is {continuity.status}")
        continuity_components = {
            "character_match": continuity.character_match,
            "location_match": continuity.location_match,
            "wardrobe_match": continuity.wardrobe_match,
            "prop_state_match": continuity.prop_state_match,
            "lighting_match": continuity.lighting_match,
            "screen_direction_match": continuity.screen_direction_match,
        }
        continuity_low = _below_threshold(continuity_components, threshold)
        if continuity_low:
            reasons.append(
                "continuity component below threshold: " + ", ".join(continuity_low)
            )
        if project.settings.provider == "google-flow" and not continuity.model_id:
            reasons.append("direct continuity QC model evidence is missing")
    elif continuity.status not in {"NotApplicable", "Passed"}:
        reasons.append(f"non-direct continuity QC is {continuity.status}")

    return reasons


def is_scene_production_ready(
    project: Project,
    scene: Scene,
    *,
    require_result: bool = True,
) -> bool:
    return not scene_production_blockers(project, scene, require_result=require_result)
