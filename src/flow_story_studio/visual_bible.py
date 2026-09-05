"""Canonical visual identity manifest and scene dependency planning."""

from __future__ import annotations

from .engines.continuity import is_direct_continuation
from .models import Project, SceneVisualPlan, VisualBible, VisualReference


def _character_lock(item) -> str:
    return (
        f"{item.id} {item.name}: {item.gender}, {item.estimated_age}, {item.build}; "
        f"face {item.face}; hair {item.hairstyle}/{item.hair_color}; eyes {item.eye_color}; "
        f"skin {item.skin_tone}; clothing {item.clothing}; accessories {item.accessories}; "
        f"identifying features {item.identifying_features}."
    )


def _location_lock(item) -> str:
    objects = ", ".join(item.objects) if item.objects else "fixed environmental objects"
    return (
        f"{item.id} {item.name}: {item.place_type}; architecture {item.architecture}; "
        f"layout {item.space}; interior {item.interior}; colors {item.colors}; "
        f"spatial anchors {item.spatial_anchors}; fixed objects {objects}."
    )


def _prop_lock(item) -> str:
    return (
        f"{item.id} {item.name}: {item.description}; owner {item.owner}; "
        f"initial location {item.initial_location}; physical state {item.state}."
    )


def build_visual_bible(project: Project) -> Project:
    existing = {item.entity_id: item for item in project.visual_bible.references}
    refs: list[VisualReference] = []
    for item in project.characters:
        refs.append(
            VisualReference(
                id=f"VIS-{item.id}",
                entity_type="character",
                entity_id=item.id,
                name=item.name,
                lock_text=_character_lock(item),
                reference_images=list(
                    dict.fromkeys(
                        [
                            *item.reference_images,
                            *(existing[item.id].reference_images if item.id in existing else []),
                        ]
                    )
                ),
                status=(
                    existing[item.id].status
                    if item.id in existing
                    else "approved"
                    if item.reference_images
                    else "missing"
                ),
                approved_reference=(
                    existing[item.id].approved_reference
                    if item.id in existing and existing[item.id].approved_reference
                    else item.reference_images[0]
                    if item.reference_images
                    else ""
                ),
                source_scene_id=(existing[item.id].source_scene_id if item.id in existing else ""),
            )
        )
    for item in project.locations:
        refs.append(
            VisualReference(
                id=f"VIS-{item.id}",
                entity_type="location",
                entity_id=item.id,
                name=item.name,
                lock_text=_location_lock(item),
                reference_images=list(
                    dict.fromkeys(
                        [
                            *item.reference_images,
                            *(existing[item.id].reference_images if item.id in existing else []),
                        ]
                    )
                ),
                status=(
                    existing[item.id].status
                    if item.id in existing
                    else "approved"
                    if item.reference_images
                    else "missing"
                ),
                approved_reference=(
                    existing[item.id].approved_reference
                    if item.id in existing and existing[item.id].approved_reference
                    else item.reference_images[0]
                    if item.reference_images
                    else ""
                ),
                source_scene_id=(existing[item.id].source_scene_id if item.id in existing else ""),
            )
        )
    for item in project.props:
        refs.append(
            VisualReference(
                id=f"VIS-{item.id}",
                entity_type="prop",
                entity_id=item.id,
                name=item.name,
                lock_text=_prop_lock(item),
                reference_images=(
                    list(existing[item.id].reference_images) if item.id in existing else []
                ),
                status=(existing[item.id].status if item.id in existing else "missing"),
                approved_reference=(
                    existing[item.id].approved_reference if item.id in existing else ""
                ),
                source_scene_id=(existing[item.id].source_scene_id if item.id in existing else ""),
            )
        )
    project.visual_bible = VisualBible(version=1, references=refs)
    by_entity = {ref.entity_id: ref for ref in refs}
    previous = None
    current_anchor = ""
    for scene in project.scenes:
        direct = is_direct_continuation(previous, scene)
        if previous is None:
            mode = "opening"
            current_anchor = scene.id
        elif direct:
            mode = "direct"
        else:
            mode = "canonical"
            current_anchor = scene.id
        char_refs = [by_entity[cid].id for cid in scene.characters if cid in by_entity]
        loc_ref = by_entity[scene.location_id].id if scene.location_id in by_entity else ""
        prop_ids = sorted(
            set(scene.start_state.prop_positions) | set(scene.end_state.prop_positions)
        )
        prop_refs = [by_entity[pid].id for pid in prop_ids if pid in by_entity]
        relevant = [by_entity[cid].lock_text for cid in scene.characters if cid in by_entity]
        if scene.location_id in by_entity:
            relevant.append(by_entity[scene.location_id].lock_text)
        relevant.extend(by_entity[pid].lock_text for pid in prop_ids if pid in by_entity)
        scene.visual_plan = SceneVisualPlan(
            dependency_mode=mode,
            anchor_scene_id=current_anchor,
            character_reference_ids=char_refs,
            location_reference_id=loc_ref,
            prop_reference_ids=prop_refs,
            lock_prompt="\n".join(relevant),
        )
        previous = scene

    for index, scene in enumerate(project.scenes):
        mode = scene.visual_plan.dependency_mode
        if mode == "opening":
            scene.start_state.notes = (
                "Opening scene; establish from canonical source truth and visual references."
            )
        elif mode == "direct":
            previous_scene = project.scenes[index - 1]
            scene.start_state.notes = (
                f"Direct continuation from {previous_scene.id}; use its accepted final frame "
                "as the physical-state anchor."
            )
        else:
            scene.start_state.notes = (
                "Canonical cut/new beat; re-anchor to this scene's source truth and canonical "
                "references. Do not inherit the previous final-frame composition."
            )

        if index + 1 >= len(project.scenes):
            scene.end_state.notes = "Final scene; no downstream frame anchor."
            continue

        next_scene = project.scenes[index + 1]
        if (
            next_scene.visual_plan.dependency_mode == "direct"
            and is_direct_continuation(scene, next_scene)
        ):
            scene.end_state.notes = (
                f"Accepted final frame may anchor {next_scene.id} because it is a direct "
                "continuation."
            )
        else:
            scene.end_state.notes = (
                f"{next_scene.id} begins as a canonical cut/new beat; do not carry this "
                "final frame forward."
            )
    return project
