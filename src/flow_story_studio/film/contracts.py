"""Compile immutable render contracts from canonical project state."""
from __future__ import annotations

import hashlib
import json

from ..models import Project, Scene
from .canonical import CanonicalFilmModel, SceneIntent


def stable_hash(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compile_render_contract(
    project: Project,
    scene: Scene,
    film_model: CanonicalFilmModel,
    intent: SceneIntent,
) -> dict[str, object]:
    character_locks = {
        item_id: film_model.characters[item_id].lock_text
        for item_id in intent.required_characters
        if item_id in film_model.characters
    }
    prop_locks = {
        item_id: film_model.props[item_id].lock_text
        for item_id in intent.required_props
        if item_id in film_model.props
    }
    location_lock = ""
    if intent.required_location in film_model.locations:
        location_lock = film_model.locations[intent.required_location].lock_text
    audio_lock = scene.orchestration.get("audio_locks", {})

    return {
        "scene_id": scene.id,
        "source_lock": scene.source_text,
        "dependency_mode": intent.dependency_mode.value,
        "character_locks": character_locks,
        "location_lock": location_lock,
        "prop_locks": prop_locks,
        "entry_state": scene.start_state.model_dump(mode="json"),
        "required_action": scene.action,
        "required_dialogue": [
            item.model_dump(mode="json") for item in scene.dialogues
        ],
        "required_voiceover": scene.voiceover,
        "audio_locks": audio_lock,
        "audio_plan": (
            audio_lock.get("scene_audio_plan", {})
            if isinstance(audio_lock, dict)
            else {}
        ),
        "camera_plan": scene.camera,
        "lighting_plan": scene.lighting,
        "atmosphere": scene.atmosphere,
        "duration": scene.duration,
        "image_plan": scene.image_plan.model_dump(
            mode="json",
            exclude={
                "status",
                "renderer_status",
                "start_frame_source",
                "reference_status",
                "approved_reference_images",
                "generated_start_frame",
                "generated_target_frame",
            },
        ),
        "expected_exit_state": scene.end_state.model_dump(mode="json"),
        "lock_mode": "strict",
        "allowed_variation": [
            "micro_expression",
            "secondary_motion",
            "cinematic_rhythm",
        ],
        "forbidden_changes": [
            "source_fact_change",
            "entity_identity_change",
            "wardrobe_change_without_state_transition",
            "prop_state_change_without_state_transition",
            "dialogue_paraphrase",
            "unrequested_character_or_location",
        ],
        "project_id": project.id,
    }


def compile_hashed_render_contract(
    project: Project,
    scene: Scene,
    film_model: CanonicalFilmModel,
    intent: SceneIntent,
) -> dict[str, object]:
    payload = compile_render_contract(project, scene, film_model, intent)
    return {
        "contract": payload,
        "contract_hash": stable_hash(payload),
    }
