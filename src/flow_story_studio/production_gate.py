"""Single fail-closed production acceptance predicate.

All downstream consumers must use this module instead of trusting mutable status flags.
"""

from __future__ import annotations

from pathlib import Path

from .film.state_delta import continuity_state_hash
from .models import Project, Scene, VisualReference
from .scene_contracts import verify_scene_contract

MASTER_BLOCKING_CODES: dict[str, set[str]] = {
    "character": {
        "identity_mismatch",
        "identity_not_established",
        "appearance_mismatch",
        "age_mismatch",
        "hair_mismatch",
        "wardrobe_mismatch",
        "wardrobe_state",
        "scene_specific_background",
        "background_contamination",
        "scene_specific_lighting",
        "lighting_mood",
        "action_pose",
        "transient_story_state",
        "readable_text",
        "logo_or_brand",
    },
    "location": {
        "layout_mismatch",
        "missing_spatial_anchor",
        "ambiguous_spatial_anchors",
        "scene_specific_weather",
        "scene_specific_lighting",
        "transient_story_prop",
        "people_present",
        "readable_text",
        "logo_or_brand",
    },
    "prop": {
        "identity_mismatch",
        "shape_mismatch",
        "material_mismatch",
        "color_mismatch",
        "state_mismatch",
        "background_contamination",
        "readable_text",
        "logo_or_brand",
    },
}


def master_reference_threshold(project: Project, reference: VisualReference) -> int:
    """Return the strict per-type floor for a Project Master.

    Character identity is reused across the most shots and therefore requires a
    stronger floor than ordinary scene QC. Location/prop Masters keep at least
    the project quality floor and never fall below 85.
    """
    base = int(project.settings.quality_threshold)
    if reference.entity_type == "character":
        return max(base, 90)
    return max(base, 85)


def is_master_blocking_issue(reference: VisualReference, code: str) -> bool:
    normalized = str(code or "").strip().casefold()
    return normalized in MASTER_BLOCKING_CODES.get(reference.entity_type, set())


def master_reference_qc_blockers(
    project: Project,
    reference: VisualReference,
) -> list[str]:
    """Return QC-only blockers for a candidate Master before approval."""
    threshold = master_reference_threshold(project, reference)
    reasons: list[str] = []
    if reference.vision_score < threshold:
        reasons.append(
            f"{reference.id} score {reference.vision_score} is below {threshold}"
        )
    if project.settings.vision_model and reference.vision_model != project.settings.vision_model:
        reasons.append(
            f"{reference.id} Vision model evidence is stale: "
            f"{reference.vision_model or '<missing>'}!={project.settings.vision_model}"
        )
    for issue in reference.vision_issues:
        if issue.severity == "error" or is_master_blocking_issue(reference, issue.code):
            reasons.append(f"{reference.id} {issue.code}: {issue.message}")
    return reasons


def master_reference_blockers(
    project: Project,
    reference: VisualReference,
    *,
    data_root: Path | None = None,
) -> list[str]:
    """Return every reason one Master is unsafe for downstream scene generation."""
    reasons = master_reference_qc_blockers(project, reference)
    if reference.status != "approved":
        reasons.append(f"{reference.id} status is {reference.status}")
    if not reference.approved_reference:
        reasons.append(f"{reference.id} approved image path is missing")
    elif data_root is not None:
        candidate = (data_root / reference.approved_reference).resolve()
        try:
            candidate.relative_to(data_root.resolve())
        except ValueError:
            reasons.append(f"{reference.id} approved image path escapes data root")
        else:
            if not candidate.is_file():
                reasons.append(f"{reference.id} approved image file is missing")
    return reasons


def project_master_blockers(
    project: Project,
    *,
    data_root: Path | None = None,
) -> list[str]:
    """Return all project-level Master Gate failures.

    Scene image/video production must not start while this list is non-empty.
    """
    reasons: list[str] = []
    for reference in project.visual_bible.references:
        reasons.extend(master_reference_blockers(project, reference, data_root=data_root))
    return reasons


def is_project_master_ready(
    project: Project,
    *,
    data_root: Path | None = None,
) -> bool:
    return not project_master_blockers(project, data_root=data_root)


def _below_threshold(values: dict[str, int], threshold: int) -> list[str]:
    return [f"{name}={score}<{threshold}" for name, score in values.items() if score < threshold]


def scene_production_score_floor(scene: Scene) -> int:
    scores: list[int] = []
    if scene.quality is not None:
        scores.append(scene.quality.score)

    scores.extend(
        [
            scene.visual_qc.score,
            scene.visual_qc.character_identity,
            scene.visual_qc.location_identity,
            scene.visual_qc.prop_consistency,
            scene.visual_qc.wardrobe_consistency,
            scene.visual_qc.lighting_consistency,
            scene.visual_qc.action_consistency,
            scene.visual_qc.composition_consistency,
        ]
    )

    if scene.audio_qc.status != "Pending":
        scores.append(scene.audio_qc.score)

    if scene.visual_plan.dependency_mode == "direct":
        scores.extend(
            [
                scene.continuity_qc.score,
                scene.continuity_qc.character_match,
                scene.continuity_qc.location_match,
                scene.continuity_qc.wardrobe_match,
                scene.continuity_qc.prop_state_match,
                scene.continuity_qc.lighting_match,
                scene.continuity_qc.screen_direction_match,
            ]
        )
    return min(scores) if scores else 0


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
    strict_floor = scene_production_score_floor(scene)
    if scene.acceptance.status == "Accepted" and scene.acceptance.score != strict_floor:
        reasons.append(
            "production acceptance score does not match strict component floor: "
            f"{scene.acceptance.score}!={strict_floor}"
        )
    if not scene.ai_locked:
        reasons.append("scene is not AI continuity locked")
    if not verify_scene_contract(scene):
        reasons.append("scene packet contract hash is missing or stale")
    if scene.acceptance.status == "Accepted":
        if scene.accepted_end_state is None:
            reasons.append("accepted end-state evidence is missing")
        elif not scene.accepted_state_hash:
            reasons.append("accepted end-state hash is missing")
        elif scene.accepted_state_hash != continuity_state_hash(scene.accepted_end_state):
            reasons.append("accepted end-state hash is stale")
    if require_result and not scene.result_file:
        reasons.append("rendered video file is missing")

    if scene.render_provider != project.settings.provider:
        reasons.append(
            "render provider does not match current project settings: "
            f"{scene.render_provider or '<missing>'}!={project.settings.provider}"
        )
    if scene.render_model != project.settings.video_model:
        reasons.append(
            "render model does not match current project settings: "
            f"{scene.render_model or '<missing>'}!={project.settings.video_model}"
        )

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

    if project.settings.provider != "mock":
        if scene.audio_qc.status != "Passed":
            reasons.append(f"audio QC is {scene.audio_qc.status}")
        if not scene.audio_qc.audio_present:
            reasons.append("rendered scene audio stream is missing")
        if scene.audio_qc.score < threshold:
            reasons.append(
                f"audio QC score {scene.audio_qc.score} is below {threshold}"
            )
        if scene.audio_qc.clipping_detected:
            reasons.append("audio true peak exceeds canonical limit")
        if not scene.audio_qc.model_id:
            reasons.append("audio QC evidence is missing")
        if not all(
            (
                scene.visual_qc.first_frame,
                scene.visual_qc.quarter_frame,
                scene.visual_qc.middle_frame,
                scene.visual_qc.three_quarter_frame,
                scene.visual_qc.last_frame,
            )
        ):
            reasons.append("visual QC five-frame evidence is incomplete")
        if not scene.visual_qc.model_id:
            reasons.append("visual QC model evidence is missing")
        if (
            project.settings.vision_model
            and scene.visual_qc.model_id != project.settings.vision_model
        ):
            reasons.append(
                "visual QC model does not match selected project Vision model: "
                f"{scene.visual_qc.model_id or '<missing>'}!="
                f"{project.settings.vision_model}"
            )

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
        if project.settings.provider != "mock" and not continuity.model_id:
            reasons.append("direct continuity QC model evidence is missing")
        if (
            project.settings.provider != "mock"
            and project.settings.vision_model
            and continuity.model_id != project.settings.vision_model
        ):
            reasons.append(
                "direct continuity QC model does not match selected project Vision model: "
                f"{continuity.model_id or '<missing>'}!="
                f"{project.settings.vision_model}"
            )
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
