"""Project-wide deterministic orchestration around AI scene enrichment."""
from __future__ import annotations

from copy import deepcopy

from ..models import Project
from .audio import scene_audio_locks
from .bridge import build_canonical_film_model, build_scene_intents
from .canonical import DependencyMode
from .contracts import compile_hashed_render_contract, stable_hash
from .state_delta import derive_scene_state_delta
from .validation import validate_project_hard_constraints


def prepare_project_orchestration(project: Project) -> Project:
    """Classify scene boundaries and enforce safe direct-entry state."""
    film_model = build_canonical_film_model(project)
    intents = build_scene_intents(project)
    previous = None

    for scene, intent in zip(project.scenes, intents, strict=True):
        mode = intent.dependency_mode
        if mode == DependencyMode.DIRECT and previous is not None:
            scene.start_state = deepcopy(previous.end_state)

        scene.visual_plan.dependency_mode = (
            "opening"
            if mode == DependencyMode.OPENING
            else "direct"
            if mode == DependencyMode.DIRECT
            else "canonical"
        )
        scene.orchestration = {
            "transition_mode": mode.value,
            "narrative_goal": intent.narrative_goal,
            "required_characters": list(intent.required_characters),
            "required_props": list(intent.required_props),
            "required_location": intent.required_location,
            "required_dialogue": list(intent.required_dialogue),
            "required_actions": list(intent.required_actions),
            "state_delta": derive_scene_state_delta(scene),
            "audio_locks": scene_audio_locks(project, scene, film_model.audio_bible),
            "authority_order": [
                "source",
                "canonical",
                "accepted_state",
                "scene_proposal",
            ],
        }
        previous = scene

    film_model_payload = film_model.model_dump(mode="json")
    project.film_model_hash = stable_hash(film_model_payload)
    project.film_model = film_model_payload
    project.film_model["scene_intents"] = [
        item.model_dump(mode="json") for item in intents
    ]
    project.film_model["state_history"] = [
        {
            "scene_id": scene.id,
            "entry": scene.start_state.model_dump(mode="json"),
            "exit": scene.end_state.model_dump(mode="json"),
        }
        for scene in project.scenes
    ]
    return project


def finalize_project_orchestration(project: Project) -> Project:
    """Compile immutable render contracts and record the hard-gate verdict."""
    film_model = build_canonical_film_model(project)
    intents = build_scene_intents(project)

    for scene, intent in zip(project.scenes, intents, strict=True):
        compiled = compile_hashed_render_contract(
            project,
            scene,
            film_model,
            intent,
        )
        scene.render_contract = compiled["contract"]
        scene.render_contract_hash = str(compiled["contract_hash"])

    verdict = validate_project_hard_constraints(project)
    film_model_payload = film_model.model_dump(mode="json")
    project.film_model_hash = stable_hash(film_model_payload)
    project.film_model = film_model_payload
    project.film_model["scene_intents"] = [
        item.model_dump(mode="json") for item in intents
    ]
    project.film_model["hard_gate"] = {
        "passed": verdict.is_valid,
        "errors": list(verdict.errors),
        "warnings": list(verdict.warnings),
    }
    project.film_model["state_history"] = [
        {
            "scene_id": scene.id,
            "transition_mode": scene.orchestration.get("transition_mode", ""),
            "entry": scene.start_state.model_dump(mode="json"),
            "exit": scene.end_state.model_dump(mode="json"),
        }
        for scene in project.scenes
    ]
    if not verdict.is_valid:
        raise ValueError(
            "Film hard-constraint validation failed: "
            + "; ".join(verdict.errors)
        )
    return project


def prepare(project: Project) -> Project:
    return prepare_project_orchestration(project)


def finalize(project: Project) -> Project:
    return finalize_project_orchestration(project)
