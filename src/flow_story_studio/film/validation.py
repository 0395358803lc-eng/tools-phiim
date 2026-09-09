"""Hard-constraint validation for film-level orchestration."""
from __future__ import annotations

from ..models import Project
from .beat_integrity import duplicate_scene_pairs
from .bridge import build_canonical_film_model
from .canonical import DependencyMode, ValidationResult
from .dependency import classify_dependency


def validate_project_hard_constraints(project: Project) -> ValidationResult:
    model = build_canonical_film_model(project)
    errors: list[str] = []
    warnings: list[str] = []
    previous = None

    if project.semantic_readiness.status != "Ready":
        if project.semantic_readiness.blockers:
            errors.extend(
                f"semantic readiness blocker: {item}"
                for item in project.semantic_readiness.blockers
            )
        else:
            errors.append("semantic readiness is not Ready")

    for scene in sorted(project.scenes, key=lambda item: item.order):
        unknown_characters = sorted(set(scene.characters) - set(model.characters))
        if unknown_characters:
            errors.append(
                f"{scene.id}: unknown characters {', '.join(unknown_characters)}"
            )
        if scene.location_id not in model.locations:
            errors.append(f"{scene.id}: unknown location {scene.location_id}")

        state_characters = (
            set(scene.start_state.character_positions)
            | set(scene.start_state.character_wardrobe)
            | set(scene.end_state.character_positions)
            | set(scene.end_state.character_wardrobe)
        )
        if not state_characters <= set(scene.characters):
            errors.append(f"{scene.id}: nested character state exceeds visible cast")
        state_props = (
            set(scene.start_state.prop_positions)
            | set(scene.end_state.prop_positions)
        )
        if not state_props <= set(model.props):
            errors.append(f"{scene.id}: state references unknown prop")

        for dialogue in scene.dialogues:
            if dialogue.character_id not in model.characters:
                errors.append(
                    f"{scene.id}: dialogue speaker {dialogue.character_id} is unknown"
                )
            if dialogue.delivery == "onscreen" and dialogue.character_id not in scene.characters:
                errors.append(
                    f"{scene.id}: onscreen speaker {dialogue.character_id} is not visible"
                )

        expected_dependency = classify_dependency(previous, scene)
        actual_dependency = scene.visual_plan.dependency_mode
        if expected_dependency == DependencyMode.DIRECT and actual_dependency != "direct":
            errors.append(
                f"{scene.id}: authored direct continuation was downgraded to {actual_dependency}"
            )
        if expected_dependency != DependencyMode.DIRECT and actual_dependency == "direct":
            errors.append(
                f"{scene.id}: direct dependency is not supported by authored scene context"
            )

        if scene.semantic_truth.frame_anchor == "previous_final_frame":
            if previous is None:
                errors.append(f"{scene.id}: opening scene cannot use previous final frame")
            elif actual_dependency != "direct":
                errors.append(
                    f"{scene.id}: previous-final-frame anchor requires direct dependency"
                )
            elif scene.start_state != previous.end_state:
                errors.append(
                    f"{previous.id}->{scene.id}: direct boundary state mismatch "
                    "(exact frame-anchor state mismatch)"
                )
        previous = scene

    for previous_id, current_id, action_score, summary_score, source_score in duplicate_scene_pairs(
        project
    ):
        errors.append(
            f"{previous_id}->{current_id}: duplicated production beat "
            f"(action={action_score:.2f}, summary={summary_score:.2f}, source={source_score:.2f})"
        )

    return ValidationResult(
        is_valid=not errors,
        errors=errors,
        warnings=warnings,
    )


def assert_project_hard_constraints(project: Project) -> Project:
    verdict = validate_project_hard_constraints(project)
    if not verdict.is_valid:
        raise ValueError(
            "Film hard-constraint validation failed: " + "; ".join(verdict.errors)
        )
    return project


_SOURCE_TRUTH_DIMENSIONS = {
    "scene_uniqueness",
    "dialogue_fidelity",
    "entity_identity",
    "location_identity",
    "physical_presence",
    "perceptual_scope",
    "ai_source_agreement",
    "source_traceability",
}
_STATE_DIMENSIONS = {
    "timeline",
    "prop_lifecycle",
    "physical_presence",
    "spatial_lifecycle",
    "part_identity",
}
_BOUNDARY_DIMENSIONS = {
    "narrative_continuity",
    "dependency_alignment",
    "frame_anchor",
}


def attach_scene_analysis_gates(
    project: Project,
    verdict: ValidationResult | None = None,
) -> Project:
    """Attach fail-closed per-scene Analysis Gate metadata."""
    verdict = verdict or validate_project_hard_constraints(project)
    hard_errors = list(verdict.errors)
    scene_gates: dict[str, dict[str, object]] = {}

    for scene in project.scenes:
        readiness_blockers = [
            item for item in project.semantic_readiness.blockers if scene.id in item
        ]
        hard_blockers = [item for item in hard_errors if scene.id in item]
        dimensions = {
            item.split(":", 1)[0].strip()
            for item in readiness_blockers
            if ":" in item
        }

        source_truth_pass = not bool(dimensions & _SOURCE_TRUTH_DIMENSIONS)
        state_pass = not bool(dimensions & _STATE_DIMENSIONS)
        boundary_pass = not bool(dimensions & _BOUNDARY_DIMENSIONS) and not any(
            marker in item.casefold()
            for item in hard_blockers
            for marker in ("dependency", "boundary", "frame-anchor")
        )
        semantic_pass = not readiness_blockers
        orchestration_pass = not hard_blockers and bool(scene.render_contract_hash)
        passed = all(
            (
                source_truth_pass,
                state_pass,
                boundary_pass,
                semantic_pass,
                orchestration_pass,
            )
        )
        blockers = list(dict.fromkeys([*readiness_blockers, *hard_blockers]))
        gate: dict[str, object] = {
            "source_truth_pass": source_truth_pass,
            "state_pass": state_pass,
            "boundary_pass": boundary_pass,
            "semantic_pass": semantic_pass,
            "orchestration_pass": orchestration_pass,
            "passed": passed,
            "blockers": blockers,
        }
        scene.orchestration["analysis_gate"] = gate
        scene_gates[scene.id] = gate

    project.film_model["analysis_gate"] = {
        "passed": bool(scene_gates) and all(bool(item["passed"]) for item in scene_gates.values()),
        "scene_count": len(scene_gates),
        "scenes": scene_gates,
    }
    return project
