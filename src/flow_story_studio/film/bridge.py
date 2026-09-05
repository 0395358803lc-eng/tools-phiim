"""Bridge existing Project data into the canonical film orchestration model."""
from __future__ import annotations

import hashlib

from ..models import Project
from .audio import build_audio_bible
from .canonical import (
    CanonicalFilmModel,
    EntityLock,
    EntityVersion,
    SceneIntent,
)
from .dependency import classify_dependency


def build_canonical_film_model(project: Project) -> CanonicalFilmModel:
    model = CanonicalFilmModel(film_id=project.id)
    model.source_facts = {
        "sha256": hashlib.sha256(project.original_text.encode("utf-8")).hexdigest(),
        "scene_count": len(project.scenes),
        "timeline": list(project.timeline),
    }
    for item in project.characters:
        lock = _character_lock(item)
        model.characters[item.id] = EntityLock(
            entity_id=item.id, source_name=item.name, lock_text=lock
        )
        model.character_versions[item.id] = [
            EntityVersion(entity_id=item.id, version_id="V1", lock_text=item.clothing)
        ]
    for item in project.locations:
        lock = _location_lock(item)
        model.locations[item.id] = EntityLock(
            entity_id=item.id, source_name=item.name, lock_text=lock
        )
        model.location_versions[item.id] = [
            EntityVersion(entity_id=item.id, version_id="V1", lock_text=lock)
        ]
    for item in project.props:
        lock = f"{item.name}: {item.description}; state={item.state}"
        model.props[item.id] = EntityLock(
            entity_id=item.id, source_name=item.name, lock_text=lock
        )
        model.prop_versions[item.id] = [
            EntityVersion(entity_id=item.id, version_id="V1", lock_text=lock)
        ]

    model.audio_bible = build_audio_bible(project)
    model.global_constraints = {
        "source_over_ai": True,
        "accepted_state_over_proposal": True,
        "no_generated_entities": True,
        "strict_dialogue_fidelity": True,
        "strict_direct_frame_anchor": True,
    }
    return model


def build_scene_intents(project: Project) -> list[SceneIntent]:
    intents: list[SceneIntent] = []
    previous = None
    for scene in project.scenes:
        props = sorted(
            set(scene.start_state.prop_positions) | set(scene.end_state.prop_positions)
        )
        intents.append(
            SceneIntent(
                scene_id=scene.id,
                narrative_goal=scene.summary,
                required_characters=list(scene.characters),
                required_props=props,
                required_location=scene.location_id,
                required_dialogue=[
                    f"{item.character_id}|{item.delivery}|{item.text}"
                    for item in scene.dialogues
                ],
                required_actions=[scene.action] if scene.action.strip() else [],
                dependency_mode=classify_dependency(previous, scene),
            )
        )
        previous = scene
    return intents


def _character_lock(item) -> str:
    return (
        f"{item.name}; {item.gender}; age={item.estimated_age}; build={item.build}; "
        f"face={item.face}; hair={item.hairstyle}/{item.hair_color}; "
        f"clothing={item.clothing}; accessories={item.accessories}; "
        f"features={item.identifying_features}"
    )


def _location_lock(item) -> str:
    objects = ", ".join(item.objects)
    return (
        f"{item.name}; type={item.place_type}; architecture={item.architecture}; "
        f"layout={item.space}; interior={item.interior}; objects={objects}; "
        f"anchors={item.spatial_anchors}"
    )
