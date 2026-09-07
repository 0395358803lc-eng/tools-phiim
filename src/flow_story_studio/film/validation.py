"""Hard-constraint validation for film-level orchestration."""
from __future__ import annotations

from ..models import Project
from .beat_integrity import duplicate_scene_pairs
from .bridge import build_canonical_film_model
from .canonical import ValidationResult


def validate_project_hard_constraints(project: Project) -> ValidationResult:
    model = build_canonical_film_model(project)
    errors: list[str] = []
    warnings: list[str] = []
    previous = None

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

        if scene.visual_plan.dependency_mode == "direct":
            if previous is None:
                errors.append(f"{scene.id}: opening scene cannot be direct")
            elif scene.start_state != previous.end_state:
                errors.append(
                    f"{previous.id}->{scene.id}: direct boundary state mismatch"
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
